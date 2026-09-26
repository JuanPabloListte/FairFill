"""python -m fleetsim [--config config.toml] [--refresh-data]"""
import argparse
import json
import time
import tomllib
from pathlib import Path

import numpy as np
import pandas as pd

from .routing import RouteProvider
from .simulate import Simulator
from .sources import fetch_raw, load_stations, load_toll_booths

ROOT = Path(__file__).resolve().parent.parent


def write_outputs(sim: Simulator, trucks, out_dir: Path):
    gt = out_dir / "_ground_truth"
    gt.mkdir(parents=True, exist_ok=True)
    frames = {k: pd.DataFrame(v) for k, v in sim.out.items()}

    # Lo que la empresa vería en sus sistemas.
    pd.DataFrame([{
        "truck_id": t.truck_id, "patente": t.patente, "tipo": t.type_name, "ejes": t.spec["axles"],
        "categoria_peaje": t.spec["toll_category"], "tanque_l": t.spec["tank_l"],
        "altura_m": t.spec["height_m"], "peso_max_t": t.spec["weight_t"], "carga_util_kg": t.spec["payload_kg"],
        "consumo_ficha_vacio_l100": t.spec["l_100km_empty"], "consumo_ficha_lleno_l100": t.spec["l_100km_full"],
        "chofer_titular": t.driver.driver_id, "tag_telepase": t.tag_id, "tarjeta_combustible": t.card_id,
        # La empresa sabe qué unidades tienen equipo de frío y de dónde toma combustible.
        "equipo_frio": t.reefer or "",
        "tanque_frio_l": sim.real["reefer_tank_l"] if t.reefer == "tanque_propio" else 0,
        "consumo_frio_lh": sim.real["reefer_l_per_h"] if t.reefer else 0,
    } for t in trucks]).to_csv(out_dir / "trucks.csv", index=False)
    frames["trips"].to_csv(out_dir / "trips.csv", index=False)
    frames["fuel_tx"].sort_values("ts").to_csv(out_dir / "fuel_transactions.csv", index=False)
    frames["tolls"].sort_values("ts").to_csv(out_dir / "toll_crossings.csv", index=False)
    frames["gps"].sort_values(["truck_id", "ts"]).to_csv(out_dir / "gps.csv.gz", index=False)
    sim.stations.drop(columns=["factor_estacion"]).to_csv(out_dir / "stations.csv", index=False)

    features = [{
        "type": "Feature",
        "properties": {"origen": o, "destino": d, "tipo": tp, "km": round(ctx.route.length_km, 1),
                       "fuente": ctx.route.source, "peajes": len(ctx.tolls), "estaciones": len(ctx.stations)},
        "geometry": {"type": "LineString",
                     "coordinates": [[round(x, 5), round(y, 5)] for x, y in zip(ctx.route.lon[::8], ctx.route.lat[::8])]},
    } for (o, d, tp), ctx in sim._route_cache.items()]
    (out_dir / "routes.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")

    # La "verdad" que el detector no debería ver.
    frames["anomalies"].sort_values("ts").to_csv(gt / "anomalies.csv", index=False)
    legit = frames["legit"] if len(frames["legit"]) else pd.DataFrame(columns=["event_id", "tipo", "truck_id", "ts"])
    legit.sort_values("ts").to_csv(gt / "legit_events.csv", index=False)
    pd.DataFrame([{
        "truck_id": t.truck_id, "ganancia": round(t.sensor.gain, 4), "offset_pct": round(t.sensor.offset_pct, 2),
        "ruido_pct": round(t.sensor.noise_pct, 2), "deriva_final_pct": round(t.sensor.drift_pct, 2),
    } for t in trucks]).to_csv(gt / "sensor_params.csv", index=False)
    pd.DataFrame({"station_id": sorted(sim.delayed_stations)}).to_csv(gt / "delayed_stations.csv", index=False)
    pd.DataFrame([{"truck_id": t.truck_id, "eficiencia_real": round(t.efficiency, 4)} for t in trucks]).to_csv(gt / "truck_params.csv", index=False)
    pd.DataFrame([{"driver_id": d.driver_id, "eficiencia_real": round(d.efficiency, 4)} for d in sim.drivers]).to_csv(gt / "driver_params.csv", index=False)
    sim.prices.events_frame().to_csv(gt / "price_increases.csv", index=False)
    return frames


def summarize(sim: Simulator, frames: dict, elapsed_s: float) -> dict:
    tx, an, trips = frames["fuel_tx"], frames["anomalies"], frames["trips"]
    bad_tx = set(an["tx_id"].dropna()) if "tx_id" in an else set()
    legit = tx[~tx["tx_id"].isin(bad_tx)]
    km = trips.groupby("truck_id")["km_odometro"].sum()
    liters = legit.groupby("truck_id")["litros"].sum()
    return {
        "segundos": round(elapsed_s, 1),
        "fuente_rutas": sim.router.provider,
        "rutas_distintas": len(sim._route_cache),
        "viajes": len(trips),
        "km_totales": round(float(km.sum())),
        "transacciones_combustible": len(tx),
        "cruces_peaje": len(frames["tolls"]),
        "gasto_peajes_ars": round(float(frames["tolls"]["importe"].sum())),
        "gasto_combustible_ars": round(float(tx["importe"].sum())),
        "puntos_gps": len(frames["gps"]),
        "anomalias_por_tipo": an["tipo"].value_counts().to_dict() if len(an) else {},
        "veces_sin_combustible": sim.stats["ran_dry"],
        "l_100km_aparente_por_camion": (liters / km * 100).round(1).dropna().to_dict(),
    }


def main():
    ap = argparse.ArgumentParser(description="Simulador de flota de camiones sobre datos reales de Argentina")
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--refresh-data", action="store_true", help="vuelve a descargar precios y peajes")
    ap.add_argument("--seed", type=int, help="reemplaza la semilla del config")
    ap.add_argument("--scenario", help="escenario de [scenarios.<nombre>] a superponer, p. ej. 'realista'")
    args = ap.parse_args()

    cfg = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
    scenarios = cfg.pop("scenarios", {})
    parts = []
    if args.scenario:
        if args.scenario not in scenarios:
            raise SystemExit(f"escenario desconocido: {args.scenario} (hay: {', '.join(scenarios)})")
        for section, values in scenarios[args.scenario].items():
            cfg[section] = {**cfg.get(section, {}), **values}
        parts.append(args.scenario)
    if args.seed is not None:
        cfg["seed"] = args.seed
        parts.append(f"seed{args.seed}")
    if parts:
        cfg["run_name"] = "_".join(parts)
    rng = np.random.default_rng(cfg["seed"])
    t0 = time.time()

    print("1/4 fuentes reales")
    raw = fetch_raw(ROOT / "data" / "raw", args.refresh_data)
    stations = load_stations(raw["precios"], cfg["fuel"], rng)
    booths = load_toll_booths(raw["peajes"])
    print(f"  {len(stations)} estaciones con {cfg['fuel']['product']}, "
          f"{int((stations['precio_base_fuente'] == 'propio_reciente').sum())} con precio reciente propio; "
          f"{len(booths)} nodos de peaje en OSM")

    print("2/4 ruteo y simulación")
    router = RouteProvider(cfg["routing"], ROOT / "data" / "cache" / "routes")
    print(f"  proveedor de rutas: {router.provider}")
    sim = Simulator(cfg, stations, booths, router, rng)
    trucks = sim.run()

    print("3/4 escritura")
    out_dir = ROOT / "output" / cfg["run_name"]
    frames = write_outputs(sim, trucks, out_dir)

    print("4/4 resumen")
    summary = summarize(sim, frames, time.time() - t0)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(f"salida en {out_dir}")


if __name__ == "__main__":
    main()
