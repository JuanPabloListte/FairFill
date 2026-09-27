"""Planificador de cargas de combustible para un viaje.

python -m planner --origen Córdoba --destino "Buenos Aires" --camion CAM-001 --carga-kg 25000
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from .map import write_map
from .service import ROOT, PlannerContext, PlannerError, _norm, plan_trip


def print_plan(res: dict, truck: dict):
    r, p = res["route"], res["problem"]
    eb, ep = res["eval_best"], res["eval_base"]
    tolls = sum(t["importe"] for t in res["tolls"])
    print(f"\n=== {res['label']}: {r.length_km:.0f} km, {r.duration_h:.1f} h de manejo ({r.source}) ===")
    print(f"consumo {res['rate'] * 100:.1f} L/100 km con margen | tanque {p.tank_l:.0f} L, sale con {p.start_l:.0f} L | "
          f"{res['corridor_n']} estaciones en el corredor, {len(res['stops'])} candidatas")
    if eb is None:
        print("NO HAY PLAN POSIBLE: no hay estaciones suficientes para ese tanque y esa reserva.")
        return
    df = pd.DataFrame(res["plan_rows"])
    if len(df):
        df["hora"] = df["hora"].dt.strftime("%d/%m %H:%M")
        df["precio"] = df.apply(lambda x: f"${x.precio:,}" + ("" if x.precio_confirmado else " (estimado)"), axis=1)
        print(df.drop(columns=["precio_confirmado", "lat", "lon", "direccion"]).to_string(index=False))
    else:
        print("no hace falta cargar: llega con lo que tiene")
    print(f"combustible ${eb['combustible']:,.0f} ({eb['litros']:.0f} L en {eb['paradas']} paradas) | "
          f"llega con {eb['llega_con_l']:.0f} L | peajes ${tolls:,.0f} ({len(res['tolls'])} cabinas)")
    if ep is not None:
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
    ap.add_argument("--salida", type=datetime.fromisoformat, help="por defecto, mañana a las 6")
    ap.add_argument("--solo-bandera", help="solo estaciones de esta bandera (p. ej. por contrato)")
    ap.add_argument("--comparar-sin-peajes", action="store_true", help="requiere ORS_API_KEY")
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    args = ap.parse_args()

    ctx = PlannerContext.load(Path(args.config))
    kwargs = dict(ctx=ctx, run_dir=Path(args.run), origen=args.origen, destino=args.destino, truck_id=args.camion,
                  carga_kg=args.carga_kg, litros_iniciales=args.litros_iniciales, salida=args.salida,
                  solo_bandera=args.solo_bandera)
    try:
        main_res, truck, origin, dest = plan_trip(**kwargs)
        results = [main_res]
        if args.comparar_sin_peajes:
            results.append(plan_trip(**kwargs, avoid_tolls=True)[0])
    except (PlannerError, FileNotFoundError, RuntimeError) as e:
        raise SystemExit(f"error: {e}")

    print(f"{truck['id']} ({truck['patente']}, {truck['tipo']}), {args.carga_kg / 1000:.1f} t: "
          f"{truck['l_100km']:.1f} L/100 km según {truck['consumption_source']}"
          + (f" + frío {truck['frio_lh']:.1f} L/h" if truck["frio_lh"] else ""))
    print(f"{origin['name']} -> {dest['name']}")
    if ctx.router.provider == "osrm":
        print("aviso: ruta de OSRM con perfil de auto; no respeta restricciones de altura ni peso (definí ORS_API_KEY)")
    for res in results:
        print_plan(res, truck)
    if len(results) == 2 and all(r["eval_best"] for r in results):
        tot = [r["eval_best"]["total"] + sum(t["importe"] for t in r["tolls"]) + r["time_cost"] for r in results]
        print(f"\ncosto total (combustible + peajes + tiempo): principal ${tot[0]:,.0f} | sin peajes ${tot[1]:,.0f}")

    out = ROOT / "output" / "planes"
    out.mkdir(parents=True, exist_ok=True)
    stem = "_".join(_norm(x).split(",")[0].replace(" ", "_") for x in (origin["name"], dest["name"])) + f"_{truck['id']}"
    (out / f"{stem}.json").write_text(json.dumps({
        "camion": truck["id"], "origen": origin, "destino": dest,
        "km": round(main_res["route"].length_km, 1), "paradas": main_res["plan_rows"],
        "peajes": main_res["tolls"], "evaluacion": main_res["eval_best"], "comparacion_chofer": main_res["eval_base"],
    }, indent=2, ensure_ascii=False, default=lambda o: o.isoformat() if isinstance(o, datetime) else float(o)),
        encoding="utf-8")
    write_map(out / f"{stem}.html", main_res, origin, dest, truck)
    print(f"\nplan y mapa en {out / stem}.json / .html")


if __name__ == "__main__":
    main()
