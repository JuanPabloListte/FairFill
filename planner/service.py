"""Lógica del planificador, sin interfaz: la usan la línea de comandos y la API."""
import tomllib
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from fleetsim.corridor import stations_on_route, tolls_on_route
from fleetsim.market import TollTariff
from fleetsim.routing import RouteProvider
from fleetsim.sources import fetch_raw, load_stations, load_toll_booths

from .optimize import Problem, Stop, simulate_policy, solve

ROOT = Path(__file__).resolve().parent.parent
GEOREF_URL = "https://apis.datos.gob.ar/georef/api/localidades"


class PlannerError(Exception):
    """Error esperable por datos de entrada; el mensaje se puede mostrar al usuario."""


class PlaceNotFound(PlannerError):
    pass


class TruckNotFound(PlannerError):
    pass


@dataclass
class PlannerContext:
    """Lo que se carga una sola vez: configuración, estaciones con precios, cabinas y ruteador."""
    cfg: dict
    stations: pd.DataFrame
    booths: pd.DataFrame
    router: RouteProvider

    @classmethod
    def load(cls, config_path: Path = ROOT / "config.toml") -> "PlannerContext":
        cfg = load_config(config_path)
        raw = fetch_raw(ROOT / "data" / "raw")
        return cls(
            cfg=cfg,
            stations=load_stations(raw["precios"], cfg["fuel"], np.random.default_rng(0)),
            booths=load_toll_booths(raw["peajes"]),
            router=RouteProvider(cfg["routing"], ROOT / "data" / "cache" / "routes"),
        )


def load_config(path: Path) -> dict:
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()


def known_places(cfg: dict) -> list[dict]:
    return [{"name": p["name"], "lat": p["lat"], "lon": p["lon"]} for p in [cfg["depot"], *cfg["destinations"]]]


def _georef(nombre: str, provincia: str | None, limit: int) -> list[dict]:
    params = {"nombre": nombre, "max": limit, **({"provincia": provincia} if provincia else {})}
    resp = requests.get(GEOREF_URL, params=params, timeout=30)
    resp.raise_for_status()
    return [{"name": f"{h['nombre'].title()}, {h['provincia']['nombre']}",
             "lat": h["centroide"]["lat"], "lon": h["centroide"]["lon"]}
            for h in resp.json().get("localidades", [])]


def geocode(name: str, cfg: dict) -> dict:
    """Primero los lugares del config; si no, la API Georef del Estado ("Localidad" o "Localidad, Provincia")."""
    for place in known_places(cfg):
        if _norm(place["name"]) == _norm(name):
            return place
    localidad, _, provincia = (x.strip() for x in name.partition(","))
    hits = _georef(localidad, provincia or None, 1)
    if not hits:
        raise PlaceNotFound(f"no encontré '{name}'; probá con 'Localidad, Provincia'")
    return hits[0]


def search_places(query: str, cfg: dict, limit: int = 8) -> list[dict]:
    """Sugerencias para autocompletar: primero los lugares del config, después Georef."""
    q = _norm(query)
    found = [p for p in known_places(cfg) if q in _norm(p["name"])]
    if len(found) < limit and len(q) >= 3:
        try:
            names = {_norm(p["name"]) for p in found}
            found += [p for p in _georef(query, None, limit) if _norm(p["name"]) not in names]
        except requests.RequestException:
            pass  # sin Georef quedan las sugerencias locales
    return found[:limit]


def load_trucks(run_dir: Path) -> pd.DataFrame:
    path = Path(run_dir) / "trucks.csv"
    if not path.exists():
        raise FileNotFoundError(f"no existe {path}: generá una corrida con `python -m fleetsim`")
    return pd.read_csv(path, keep_default_na=False).set_index("truck_id")


def load_consumption_model(run_dir: Path) -> pd.DataFrame | None:
    path = Path(run_dir) / "consumption_model.csv"
    return pd.read_csv(path).set_index("truck_id") if path.exists() else None


def truck_profile(run_dir: Path, truck_id: str, carga_kg: float, cfg: dict) -> dict:
    """Ficha del camión y su consumo: el aprendido por el detector si existe, si no la ficha técnica."""
    trucks = load_trucks(run_dir)
    if truck_id not in trucks.index:
        raise TruckNotFound(f"no existe el camión {truck_id}")
    t = trucks.loc[truck_id]
    tons = carga_kg / 1000
    load_ratio = min(carga_kg / t["carga_util_kg"], 1.0)
    l100 = t["consumo_ficha_vacio_l100"] + (t["consumo_ficha_lleno_l100"] - t["consumo_ficha_vacio_l100"]) * load_ratio
    frio_lh = float(t.get("consumo_frio_lh", 0) or 0) if t.get("equipo_frio") == "tanque_principal" else 0.0
    source = "ficha técnica"
    m = load_consumption_model(run_dir)
    if m is not None and truck_id in m.index and pd.notna(m.at[truck_id, "l_100km_vacio"]):
        l100 = m.at[truck_id, "l_100km_vacio"] + m.at[truck_id, "l_100tkm"] * tons
        frio_lh = float(m.at[truck_id, "frio_lh"]) if frio_lh else 0.0
        source = f"modelo de consumo del detector ({int(m.at[truck_id, 'pares'])} pares tanque lleno)"
    if carga_kg <= 0:
        frio_lh = 0.0  # el equipo de frío solo funciona con carga
    return {"id": truck_id, "patente": t["patente"], "tipo": t["tipo"], "tank_l": float(t["tanque_l"]),
            "toll_category": t["categoria_peaje"], "spec": dict(cfg["truck_types"][t["tipo"]]),
            "l_100km": float(l100), "frio_lh": frio_lh, "consumption_source": source,
            "threshold": cfg["planner"]["policy_threshold"]}


def build_stops(corridor: pd.DataFrame, pc: dict, fuel: dict, allowed_brand: str | None):
    st = corridor.copy()
    if allowed_brand:
        st = st[st["bandera"].str.upper().str.contains(allowed_brand.upper(), regex=False)]
    if st.empty:
        return [], st.assign(precio_esperado=[], precio_confirmado=[], precio_decision=[])
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


def evaluate(plan: dict, stops: list[Stop], p: Problem) -> dict | None:
    """Costo de un plan con precios esperados (sin la prima de riesgo usada para decidir). None si no es factible."""
    if not np.isfinite(plan["cost"]):
        return None
    fuel = sum(l * stops[j].ref.precio_esperado for j, l, _ in plan["plan"])
    stop_cost = sum(stops[j].stop_cost for j, _, _ in plan["plan"])
    # Costo del viaje: lo comprado + las paradas, corregido por cuánto cambió el tanque entre salida y llegada.
    inventory = (plan["end_l"] - p.start_l) * p.end_value
    return {"combustible": fuel, "costo_paradas": stop_cost, "variacion_tanque": inventory,
            "total": fuel + stop_cost - inventory, "paradas": len(plan["plan"]),
            "litros": sum(l for _, l, _ in plan["plan"]), "llega_con_l": plan["end_l"]}


def plan_route(route, ctx: PlannerContext, truck: dict, start_l: float | None, depart: datetime,
               allowed_brand: str | None = None, label: str = "Ruta principal") -> dict:
    cfg = ctx.cfg
    pc, fuel = cfg["planner"], cfg["fuel"]
    speed = route.length_km / route.duration_h
    rate = (truck["l_100km"] / 100 + truck["frio_lh"] / speed) * pc["consumption_margin"]
    corridor = stations_on_route(route, ctx.stations, fuel["corridor_km"])
    stops, table = build_stops(corridor, pc, fuel, allowed_brand)
    tank = truck["tank_l"]
    # Lo que sobra en el tanque vale lo que costaría reponerlo cerca del destino (con descuento), un
    # poco menos: así el plan no llena de más "para especular" con un precio que no es seguro.
    near_end = table[table["km"] > route.length_km - 100]
    ref_prices = (near_end if len(near_end) else table)["precio_esperado"]
    end_value = float(ref_prices.quantile(0.25)) * 0.99 if len(ref_prices) else 0.0
    p = Problem(length_km=route.length_km, rate_l_km=rate, tank_l=tank,
                start_l=start_l if start_l is not None else tank / 2,
                reserve_l=pc["reserve_frac"] * tank, end_reserve_l=pc["end_reserve_frac"] * tank,
                end_value=end_value, min_buy_l=pc.get("min_buy_l", 0.0))
    best = solve(p, stops)
    base = simulate_policy(p, stops, pc["policy_threshold"], lambda s: s.ref.bandera == fuel["preferred_brand"])

    tariff = TollTariff(cfg["tolls"])
    toll_rows = []
    for t in tolls_on_route(route, ctx.booths, cfg["tolls"]["match_radius_km"], cfg["tolls"]["merge_km"]):
        when = (depart + timedelta(hours=t["km"] / speed)).replace(second=0, microsecond=0)
        amount, peak = tariff.amount(truck["toll_category"], when)
        toll_rows.append({**t, "hora": when, "importe": amount, "pico": peak})

    rows, elapsed_stop_h = [], 0.0
    for j, liters, arrival in best.get("plan", []):
        r = stops[j].ref
        eta = (depart + timedelta(hours=r.km / speed + elapsed_stop_h)).replace(second=0, microsecond=0)
        elapsed_stop_h += pc["stop_minutes"] / 60
        rows.append({
            "km": round(float(r.km), 1), "hora": eta, "estacion": r.empresa, "bandera": r.bandera,
            "direccion": r.direccion, "localidad": f"{r.localidad}, {r.provincia}".title(),
            "desvio_km": round(float(r.offset_km), 1), "llega_con_l": round(float(arrival)),
            "cargar_l": round(float(liters)), "precio": round(float(r.precio_esperado)),
            "precio_confirmado": bool(r.precio_confirmado), "lat": float(r.lat), "lon": float(r.lon),
        })
    return {"label": label, "route": route, "problem": p, "rate": rate, "stops": stops, "candidates": table,
            "corridor_n": len(corridor), "best": best, "base": base, "plan_rows": rows, "tolls": toll_rows,
            "eval_best": evaluate(best, stops, p), "eval_base": evaluate(base, stops, p),
            "time_cost": route.duration_h * pc["cost_per_hour_ars"]}


def plan_trip(ctx: PlannerContext, run_dir: Path, origen: str, destino: str, truck_id: str, carga_kg: float = 0.0,
              litros_iniciales: float | None = None, salida: datetime | None = None,
              solo_bandera: str | None = None, avoid_tolls: bool = False) -> tuple[dict, dict, dict, dict]:
    """Planifica un viaje de punta a punta. Devuelve (resultado, camión, origen, destino)."""
    truck = truck_profile(run_dir, truck_id, carga_kg, ctx.cfg)
    if litros_iniciales is not None and litros_iniciales > truck["tank_l"]:
        raise PlannerError(f"el tanque de {truck_id} es de {truck['tank_l']:.0f} L")
    origin, dest = geocode(origen, ctx.cfg), geocode(destino, ctx.cfg)
    depart = salida or datetime.combine(datetime.now().date() + timedelta(days=1), datetime.min.time()).replace(hour=6)
    route = ctx.router.route(origin, dest, truck["spec"], avoid_tolls=avoid_tolls)
    label = "Ruta sin peajes" if avoid_tolls else "Ruta principal"
    return plan_route(route, ctx, truck, litros_iniciales, depart, solo_bandera, label), truck, origin, dest
