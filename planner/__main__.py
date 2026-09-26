"""Planificador de cargas de combustible para un viaje.

python -m planner --origen Córdoba --destino "Buenos Aires" --camion CAM-001 --carga-kg 25000
"""
import argparse
import json
import tomllib
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from fleetsim.corridor import stations_on_route, tolls_on_route
from fleetsim.market import TollTariff
from fleetsim.routing import RouteProvider
from fleetsim.sources import fetch_raw, load_stations, load_toll_booths

from .map import write_map
from .optimize import Problem, Stop, simulate_policy, solve

ROOT = Path(__file__).resolve().parent.parent
GEOREF_URL = "https://apis.datos.gob.ar/georef/api/localidades"


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()


def geocode(name: str, cfg: dict) -> dict:
    """Primero los lugares del config; si no, la API Georef del Estado ("Localidad" o "Localidad, Provincia")."""
    for place in [cfg["depot"], *cfg["destinations"]]:
        if _norm(place["name"]) == _norm(name):
            return {"name": place["name"], "lat": place["lat"], "lon": place["lon"]}
    localidad, _, provincia = (x.strip() for x in name.partition(","))
    params = {"nombre": localidad, "max": 1, **({"provincia": provincia} if provincia else {})}
    resp = requests.get(GEOREF_URL, params=params, timeout=30)
    resp.raise_for_status()
    hits = resp.json().get("localidades", [])
    if not hits:
        raise SystemExit(f"no encontré '{name}' en Georef; probá con 'Localidad, Provincia'")
    h = hits[0]
    return {"name": f"{h['nombre'].title()}, {h['provincia']['nombre']}",
            "lat": h["centroide"]["lat"], "lon": h["centroide"]["lon"]}


def truck_profile(args, cfg) -> dict:
    """Ficha del camión y su consumo: el aprendido por el detector si existe, si no la ficha técnica."""
    run_dir = Path(args.run)
    trucks = pd.read_csv(run_dir / "trucks.csv", keep_default_na=False).set_index("truck_id")
    if args.camion not in trucks.index:
        raise SystemExit(f"{args.camion} no está en {run_dir / 'trucks.csv'}")
    t = trucks.loc[args.camion]
    spec = dict(cfg["truck_types"][t["tipo"]])
    tons = args.carga_kg / 1000
    load_ratio = min(args.carga_kg / t["carga_util_kg"], 1.0)
    l100 = t["consumo_ficha_vacio_l100"] + (t["consumo_ficha_lleno_l100"] - t["consumo_ficha_vacio_l100"]) * load_ratio
    source, frio_lh = "ficha técnica", float(t.get("consumo_frio_lh", 0) or 0) if t.get("equipo_frio") == "tanque_principal" else 0.0
    model_path = run_dir / "consumption_model.csv"
    if model_path.exists():
        m = pd.read_csv(model_path).set_index("truck_id")
        if args.camion in m.index and pd.notna(m.at[args.camion, "l_100km_vacio"]):
            l100 = m.at[args.camion, "l_100km_vacio"] + m.at[args.camion, "l_100tkm"] * tons
            frio_lh = float(m.at[args.camion, "frio_lh"]) if frio_lh else 0.0
            source = f"modelo de consumo del detector ({int(m.at[args.camion, 'pares'])} pares tanque lleno)"
    if args.carga_kg <= 0:
        frio_lh = 0.0  # el equipo de frío solo funciona con carga
    return {"id": args.camion, "patente": t["patente"], "tipo": t["tipo"], "tank_l": float(t["tanque_l"]),
            "toll_category": t["categoria_peaje"], "spec": spec, "l_100km": float(l100),
            "frio_lh": frio_lh, "consumption_source": source}


def build_stops(corridor: pd.DataFrame, pc: dict, fuel: dict, rate: float, allowed_brand: str | None):
    st = corridor.copy()
    if allowed_brand:
        st = st[st["bandera"].str.upper().str.contains(allowed_brand.upper())]
    disc = np.where(st["bandera"] == fuel["preferred_brand"], 1 - fuel["preferred_discount"], 1.0)
    st["precio_esperado"] = st["precio_base"] * disc
    st["precio_confirmado"] = st["precio_base_fuente"] == "propio_reciente"
    st["precio_decision"] = st["precio_esperado"] * np.where(st["precio_confirmado"], 1.0, 1 + pc["estimated_price_risk"])
    # La más barata (para decidir) de cada tramo: reduce el problema sin perder las buenas opciones.
    st["tramo"] = (st["km"] // pc["bucket_km"]).astype(int)
    st = st.loc[st.groupby("tramo")["precio_decision"].idxmin()].sort_values("km").reset_index(drop=True)
    hour_cost = pc["cost_per_hour_ars"]
    stops = [Stop(
        km=r.km, offset_km=r.offset_km, price=r.precio_decision,
        stop_cost=hour_cost * (pc["stop_minutes"] / 60 + 2 * r.offset_km / 40), ref=r,
    ) for r in st.itertuples()]
    return stops, st


def evaluate(plan: dict, stops: list[Stop], p: Problem) -> dict:
    """Costo de un plan con precios esperados (sin la prima de riesgo usada para decidir)."""
    if not np.isfinite(plan["cost"]):
        return {"combustible": np.inf, "paradas": 0, "litros": 0}
    fuel = sum(l * stops[j].ref.precio_esperado for j, l, _ in plan["plan"])
    stop_cost = sum(stops[j].stop_cost for j, _, _ in plan["plan"])
    # Costo del viaje: lo comprado + las paradas, corregido por cuánto cambió el tanque entre salida y llegada.
    inventory = (plan["end_l"] - p.start_l) * p.end_value
    return {"combustible": fuel, "costo_paradas": stop_cost, "variacion_tanque": inventory,
            "total": fuel + stop_cost - inventory, "paradas": len(plan["plan"]),
            "litros": sum(l for _, l, _ in plan["plan"]), "llega_con_l": plan["end_l"]}


def plan_route(route, stations, booths, truck, args, cfg, label: str) -> dict:
    pc, fuel = cfg["planner"], cfg["fuel"]
    speed = route.length_km / route.duration_h
    rate = (truck["l_100km"] / 100 + truck["frio_lh"] / speed) * pc["consumption_margin"]
    corridor = stations_on_route(route, stations, fuel["corridor_km"])
    stops, table = build_stops(corridor, pc, fuel, rate, args.solo_bandera)
    tank = truck["tank_l"]
    # Lo que sobra en el tanque vale lo que costaría reponerlo cerca del destino (con descuento), un
    # poco menos: así el plan no llena de más "para especular" con un precio que no es seguro.
    near_end = table[table["km"] > route.length_km - 100]
    end_value = float((near_end if len(near_end) else table)["precio_esperado"].quantile(0.25)) * 0.99
    p = Problem(length_km=route.length_km, rate_l_km=rate, tank_l=tank,
                start_l=args.litros_iniciales if args.litros_iniciales is not None else tank / 2,
                reserve_l=pc["reserve_frac"] * tank, end_reserve_l=pc["end_reserve_frac"] * tank,
                end_value=end_value)
    best = solve(p, stops)
    prefer = (lambda s: s.ref.bandera == fuel["preferred_brand"])
    base = simulate_policy(p, stops, pc["policy_threshold"], prefer)

    tariff = TollTariff(cfg["tolls"])
    tolls = tolls_on_route(route, booths, cfg["tolls"]["match_radius_km"], cfg["tolls"]["merge_km"])
    depart = datetime.fromisoformat(args.salida)
    toll_rows = []
    for t in tolls:
        when = depart + timedelta(hours=t["km"] / speed)
        amount, peak = tariff.amount(truck["toll_category"], when)
        toll_rows.append({**t, "hora": when, "importe": amount, "pico": peak})

    rows, elapsed_stop_h = [], 0.0
    for j, liters, arrival in best.get("plan", []):
        r = stops[j].ref
        eta = depart + timedelta(hours=r.km / speed + elapsed_stop_h)
        elapsed_stop_h += pc["stop_minutes"] / 60
        rows.append({
            "km": round(r.km), "hora": eta.strftime("%d/%m %H:%M"), "estacion": r.empresa, "bandera": r.bandera,
            "localidad": f"{r.localidad}, {r.provincia}".title(), "desvio_km": round(r.offset_km, 1),
            "llega_con_l": round(arrival), "cargar_l": round(liters),
            "precio": round(r.precio_esperado), "precio_confirmado": bool(r.precio_confirmado),
            "lat": r.lat, "lon": r.lon,
        })
    return {"label": label, "route": route, "problem": p, "rate": rate, "stops": stops, "candidates": table,
            "corridor_n": len(corridor), "best": best, "base": base, "plan_rows": rows, "tolls": toll_rows,
            "eval_best": evaluate(best, stops, p), "eval_base": evaluate(base, stops, p),
            "time_cost": route.duration_h * pc["cost_per_hour_ars"]}


def print_plan(res: dict, truck: dict):
    r, p = res["route"], res["problem"]
    eb, ep = res["eval_best"], res["eval_base"]
    tolls = sum(t["importe"] for t in res["tolls"])
    print(f"\n=== {res['label']}: {r.length_km:.0f} km, {r.duration_h:.1f} h de manejo ({r.source}) ===")
    print(f"consumo {res['rate'] * 100:.1f} L/100 km con margen | tanque {p.tank_l:.0f} L, sale con {p.start_l:.0f} L | "
          f"{res['corridor_n']} estaciones en el corredor, {len(res['stops'])} candidatas")
    if not np.isfinite(res["best"]["cost"]):
        print("NO HAY PLAN POSIBLE: no hay estaciones suficientes para ese tanque y esa reserva.")
        return
    df = pd.DataFrame(res["plan_rows"]).drop(columns=["lat", "lon"])
    if len(df):
        df["precio"] = df.apply(lambda x: f"${x.precio:,}" + ("" if x.precio_confirmado else " (estimado)"), axis=1)
        print(df.drop(columns=["precio_confirmado"]).to_string(index=False))
    else:
        print("no hace falta cargar: llega con lo que tiene")
    print(f"combustible ${eb['combustible']:,.0f} ({eb['litros']:.0f} L en {eb['paradas']} paradas) | "
          f"llega con {eb['llega_con_l']:.0f} L | peajes ${tolls:,.0f} ({len(res['tolls'])} cabinas)")
    if np.isfinite(ep["total"]):
        diff = ep["total"] - eb["total"]
        print(f"costo del viaje (combustible consumido + paradas): ${eb['total']:,.0f} | cargando al bajar del "
              f"{truck['threshold'] * 100:.0f}% en la primera estación (preferida si hay): ${ep['total']:,.0f} "
              f"en {ep['paradas']} paradas -> ahorro ${diff:,.0f} ({diff / ep['total'] * 100:.1f}%)")
    n_est = sum(not x["precio_confirmado"] for x in res["plan_rows"])
    if n_est:
        print(f"ojo: {n_est} parada(s) con precio estimado; la estación no informa su precio hace más de 60 días")


def main():
    ap = argparse.ArgumentParser(description="Dónde y cuánto cargar gasoil en un viaje")
    ap.add_argument("--origen", required=True)
    ap.add_argument("--destino", required=True)
    ap.add_argument("--camion", default="CAM-001")
    ap.add_argument("--run", default=str(ROOT / "output" / "realista"), help="corrida con trucks.csv y el modelo de consumo")
    ap.add_argument("--carga-kg", type=float, default=0.0)
    ap.add_argument("--litros-iniciales", type=float)
    ap.add_argument("--salida", default=(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT06:00"))
    ap.add_argument("--solo-bandera", help="solo estaciones de esta bandera (p. ej. por contrato)")
    ap.add_argument("--comparar-sin-peajes", action="store_true", help="requiere ORS_API_KEY")
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    args = ap.parse_args()

    cfg = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
    rng = np.random.default_rng(0)
    raw = fetch_raw(ROOT / "data" / "raw")
    stations = load_stations(raw["precios"], cfg["fuel"], rng)
    booths = load_toll_booths(raw["peajes"])
    truck = truck_profile(args, cfg)
    truck["threshold"] = cfg["planner"]["policy_threshold"]
    origin, dest = geocode(args.origen, cfg), geocode(args.destino, cfg)
    router = RouteProvider(cfg["routing"], ROOT / "data" / "cache" / "routes")

    print(f"{truck['id']} ({truck['patente']}, {truck['tipo']}), {args.carga_kg / 1000:.1f} t: "
          f"{truck['l_100km']:.1f} L/100 km según {truck['consumption_source']}"
          + (f" + frío {truck['frio_lh']:.1f} L/h" if truck["frio_lh"] else ""))
    print(f"{origin['name']} -> {dest['name']}, salida {args.salida}")
    if router.provider == "osrm":
        print("aviso: ruta de OSRM con perfil de auto; no respeta restricciones de altura ni peso (definí ORS_API_KEY)")

    results = [plan_route(router.route(origin, dest, truck["spec"]), stations, booths, truck, args, cfg, "Ruta principal")]
    if args.comparar_sin_peajes:
        results.append(plan_route(router.route(origin, dest, truck["spec"], avoid_tolls=True),
                                  stations, booths, truck, args, cfg, "Ruta sin peajes"))
    for res in results:
        print_plan(res, truck)
    if len(results) == 2 and all(np.isfinite(r["best"]["cost"]) for r in results):
        tot = [r["eval_best"]["total"] + sum(t["importe"] for t in r["tolls"]) + r["time_cost"] for r in results]
        print(f"\ncosto total (combustible + peajes + tiempo): principal ${tot[0]:,.0f} | sin peajes ${tot[1]:,.0f}")

    out = ROOT / "output" / "planes"
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{_norm(origin['name']).split(',')[0].replace(' ', '_')}_{_norm(dest['name']).split(',')[0].replace(' ', '_')}_{truck['id']}"
    main_res = results[0]
    (out / f"{stem}.json").write_text(json.dumps({
        "camion": truck["id"], "origen": origin, "destino": dest, "salida": args.salida,
        "km": round(main_res["route"].length_km, 1), "paradas": main_res["plan_rows"],
        "peajes": [{k: v for k, v in t.items() if k != "hora"} | {"hora": t["hora"].isoformat()} for t in main_res["tolls"]],
        "evaluacion": main_res["eval_best"], "comparacion_chofer": main_res["eval_base"],
    }, indent=2, ensure_ascii=False, default=float), encoding="utf-8")
    write_map(out / f"{stem}.html", main_res, origin, dest, truck)
    print(f"\nplan y mapa en {out / stem}.json / .html")


if __name__ == "__main__":
    main()
