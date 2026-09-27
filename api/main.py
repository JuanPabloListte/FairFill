"""API de FairFill.

uvicorn api.main:app --reload            # documentación interactiva en http://localhost:8000/docs

La corrida (datos de la flota y salida del detector) se elige con FAIRFILL_RUN; por defecto output/realista.
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from planner.service import (ROOT, PlaceNotFound, PlannerContext, PlannerError, TruckNotFound,
                             load_consumption_model, load_trucks, plan_trip, search_places)

from .schemas import (Alerta, Camion, CamionPlan, Comparacion, Costos, Lugar, Parada, Peaje, PlanRequest,
                      PlanResponse, ResumenAlertas, Ruta, Salud)

MAX_GEOMETRY_POINTS = 600


def create_app(context: PlannerContext | None = None, run_dir: Path | None = None) -> FastAPI:
    """`context` y `run_dir` se pueden inyectar (tests); si no, se cargan al arrancar."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ctx = context or PlannerContext.load()
        app.state.run_dir = Path(run_dir or os.environ.get("FAIRFILL_RUN", ROOT / "output" / "realista"))
        yield

    app = FastAPI(
        title="FairFill",
        description="Dónde y cuánto cargar gasoil en cada viaje, y qué cargas no cierran.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.environ.get("FAIRFILL_CORS", "http://localhost:5173").split(","),
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    def ctx(request: Request) -> PlannerContext:
        return request.app.state.ctx

    def run(request: Request) -> Path:
        return request.app.state.run_dir

    def trucks_or_503(request: Request) -> pd.DataFrame:
        try:
            return load_trucks(run(request))
        except FileNotFoundError as e:
            raise HTTPException(503, str(e))

    @app.get("/salud", response_model=Salud, tags=["sistema"])
    def salud(request: Request):
        c = ctx(request)
        return Salud(estado="ok", proveedor_rutas=c.router.provider, estaciones=len(c.stations),
                     cabinas_peaje=len(c.booths), corrida=run(request).name,
                     corrida_disponible=(run(request) / "trucks.csv").exists())

    @app.get("/lugares", response_model=list[Lugar], tags=["planificador"])
    def lugares(request: Request, q: str = Query(min_length=2, description="parte del nombre de una localidad"),
                limite: int = Query(8, ge=1, le=20)):
        return [Lugar(nombre=p["name"], lat=p["lat"], lon=p["lon"]) for p in search_places(q, ctx(request).cfg, limite)]

    @app.get("/camiones", response_model=list[Camion], tags=["flota"])
    def camiones(request: Request):
        trucks = trucks_or_503(request)
        return [_camion(tid, row, load_consumption_model(run(request))) for tid, row in trucks.iterrows()]

    @app.get("/camiones/{camion_id}", response_model=Camion, tags=["flota"])
    def camion(camion_id: str, request: Request):
        trucks = trucks_or_503(request)
        if camion_id not in trucks.index:
            raise HTTPException(404, f"no existe el camión {camion_id}")
        return _camion(camion_id, trucks.loc[camion_id], load_consumption_model(run(request)))

    @app.post("/planes", response_model=PlanResponse, tags=["planificador"])
    def planes(req: PlanRequest, request: Request):
        c = ctx(request)
        try:
            res, truck, origin, dest = plan_trip(
                c, run(request), req.origen, req.destino, req.camion_id, req.carga_kg, req.litros_iniciales,
                req.salida, req.solo_bandera, req.evitar_peajes)
        except TruckNotFound as e:
            raise HTTPException(404, str(e))
        except (PlaceNotFound, PlannerError) as e:
            raise HTTPException(422, str(e))
        except FileNotFoundError as e:
            raise HTTPException(503, str(e))
        except RuntimeError as e:  # ruteo: servicio externo caído o función no disponible
            raise HTTPException(502, str(e))
        return _plan_response(res, truck, origin, dest, c)

    @app.get("/alertas", response_model=list[Alerta], tags=["control de cargas"])
    def alertas(request: Request, tipo: str | None = None, camion_id: str | None = None,
                desde: datetime | None = None, hasta: datetime | None = None,
                limite: int = Query(200, ge=1, le=5000)):
        df = _alerts(run(request))
        if tipo:
            df = df[df["tipo"] == tipo]
        if camion_id:
            df = df[df["truck_id"] == camion_id]
        if desde:
            df = df[df["ts"] >= desde]
        if hasta:
            df = df[df["ts"] <= hasta]
        df = df.sort_values("ts", ascending=False).head(limite)
        return [Alerta(**{k: (None if pd.isna(v) else v) for k, v in r.items()}) for r in df.to_dict("records")]

    @app.get("/alertas/resumen", response_model=ResumenAlertas, tags=["control de cargas"])
    def resumen(request: Request):
        df = _alerts(run(request))
        return ResumenAlertas(total=len(df), ars_en_riesgo=float(df["ars_en_riesgo"].sum()),
                              por_tipo=df["tipo"].value_counts().to_dict(),
                              por_camion=df["truck_id"].value_counts().to_dict())

    return app


def _camion(tid: str, t: pd.Series, model: pd.DataFrame | None) -> Camion:
    m = model.loc[tid] if model is not None and tid in model.index else None
    fitted = m is not None and pd.notna(m["l_100km_vacio"])
    return Camion(
        id=tid, patente=t["patente"], tipo=t["tipo"], ejes=int(t["ejes"]), categoria_peaje=t["categoria_peaje"],
        tanque_l=float(t["tanque_l"]), carga_util_kg=float(t["carga_util_kg"]),
        equipo_frio=t.get("equipo_frio") or None,
        consumo_ficha_vacio_l100=float(t["consumo_ficha_vacio_l100"]),
        consumo_ficha_lleno_l100=float(t["consumo_ficha_lleno_l100"]),
        consumo_modelo_vacio_l100=float(m["l_100km_vacio"]) if fitted else None,
        consumo_modelo_l100_por_tonelada=float(m["l_100tkm"]) if fitted else None,
        pares_modelo=int(m["pares"]) if m is not None else None,
    )


def _alerts(run_dir: Path) -> pd.DataFrame:
    path = run_dir / "alerts.csv"
    if not path.exists():
        raise HTTPException(503, f"no hay alertas en {run_dir.name}: corré `python -m detector --run {run_dir}`")
    df = pd.read_csv(path, parse_dates=["ts"], dtype={"tx_id": "string"})
    return df[["alert_id", "truck_id", "ts", "tipo", "tx_id", "litros_en_riesgo", "ars_en_riesgo", "explicacion"]]


def _plan_response(res: dict, truck: dict, origin: dict, dest: dict, ctx: PlannerContext) -> PlanResponse:
    route, p = res["route"], res["problem"]
    step = max(len(route.lat) // MAX_GEOMETRY_POINTS, 1)
    geometry = [(round(float(a), 5), round(float(b), 5)) for a, b in zip(route.lat[::step], route.lon[::step])]
    geometry[-1] = (round(float(route.lat[-1]), 5), round(float(route.lon[-1]), 5))
    tolls_total = float(sum(t["importe"] for t in res["tolls"]))
    eb, ep = res["eval_best"], res["eval_base"]

    avisos = []
    if route.source == "osrm":
        avisos.append("Ruta calculada con perfil de auto (OSRM): no respeta restricciones de altura ni peso.")
    if eb is None:
        avisos.append("No hay plan posible: faltan estaciones para ese tanque y esa reserva.")
    n_est = sum(not r["precio_confirmado"] for r in res["plan_rows"])
    if n_est:
        avisos.append(f"{n_est} parada(s) con precio estimado: la estación no informa su precio hace más de 60 días.")

    costos = comparacion = None
    if eb:
        costos = Costos(combustible=eb["combustible"], costo_paradas=eb["costo_paradas"],
                        variacion_tanque=eb["variacion_tanque"], total_viaje=eb["total"], peajes=tolls_total,
                        litros=eb["litros"], llega_con_l=eb["llega_con_l"])
        if ep:
            comparacion = Comparacion(
                politica=f"cargar al bajar del {truck['threshold'] * 100:.0f}% en la primera estación "
                         f"({ctx.cfg['fuel']['preferred_brand']} si hay)",
                total_viaje=ep["total"], paradas=ep["paradas"], ahorro=ep["total"] - eb["total"],
                ahorro_pct=(ep["total"] - eb["total"]) / ep["total"] * 100)

    return PlanResponse(
        factible=eb is not None,
        origen=Lugar(nombre=origin["name"], lat=origin["lat"], lon=origin["lon"]),
        destino=Lugar(nombre=dest["name"], lat=dest["lat"], lon=dest["lon"]),
        camion=CamionPlan(id=truck["id"], patente=truck["patente"], tipo=truck["tipo"], tanque_l=truck["tank_l"],
                          consumo_l_100km=truck["l_100km"], consumo_frio_lh=truck["frio_lh"],
                          fuente_consumo=truck["consumption_source"]),
        ruta=Ruta(km=route.length_km, horas_manejo=route.duration_h, fuente=route.source, geometria=geometry),
        sale_con_l=p.start_l,
        paradas=[Parada(**r) for r in res["plan_rows"]],
        peajes=[Peaje(km=float(t["km"]), hora=t["hora"], nombre=t["nombre"] if isinstance(t["nombre"], str) else None,
                      operador=t["operador"] if isinstance(t["operador"], str) else None, importe=float(t["importe"]),
                      pico=bool(t["pico"]), lat=float(t["lat"]), lon=float(t["lon"])) for t in res["tolls"]],
        costos=costos, comparacion=comparacion, avisos=avisos,
    )


app = create_app()
