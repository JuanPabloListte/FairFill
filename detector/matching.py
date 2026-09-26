"""Cargas físicas detectadas en el sensor y su emparejamiento con las transacciones de las tarjetas.

Idea: en lugar de comparar el sensor justo antes y después de cada transacción (frágil ante
relojes desfasados y estaciones que informan tarde), primero se detectan las subidas de nivel
con el camión detenido y después se asigna cada transacción a una de esas cargas, por lugar
y dentro de una ventana de tiempo amplia.
"""
import numpy as np
import pandas as pd

from .features import haversine_km

MATCH_RADIUS_KM = 3.0
MAX_POSTING_DELAY = pd.Timedelta(hours=14)   # demora máxima con que una estación informa
CLOCK_TOLERANCE = pd.Timedelta(minutes=30)   # la transacción puede figurar antes que la subida


def mark_stuck(gps: pd.DataFrame, min_run: int = 3) -> pd.Series:
    """Lecturas repetidas idénticas (sensor trabado). Se conserva la primera de cada racha.

    No cuenta las lecturas saturadas en 0 o 100, que se repiten por el tope del sensor y no por falla.
    """
    g = gps.sort_values(["truck_id", "ts"])
    lv = g["fuel_level_pct"]
    same = (lv == lv.shift()) & (g["truck_id"] == g["truck_id"].shift()) & lv.between(0.05, 99.95)
    run = (~same).cumsum()
    run_len = same.groupby(run).transform("sum") + 1
    stuck = same & (run_len >= min_run)
    return stuck.reindex(gps.index)


def sensor_noise_by_truck(gps: pd.DataFrame) -> pd.Series:
    """Desvío del ruido de cada sensor, con lecturas consecutivas válidas y el camión detenido."""
    g = gps[~gps["trabado"]].sort_values(["truck_id", "ts"])
    same = (g["truck_id"] == g["truck_id"].shift()) & ~g["ignition"] & ~g["ignition"].shift(fill_value=True)
    diff = g["fuel_level_pct"].diff().where(same)

    def robust_sd(d):
        d = d.dropna()
        return 1.4826 * np.median(np.abs(d - np.median(d))) / np.sqrt(2)

    return diff.groupby(g["truck_id"]).apply(robust_sd)


def _off_runs(g: pd.DataFrame):
    """Índices de cada período con el motor apagado, extendidos 2 lecturas hacia cada lado."""
    run_id = (g["ignition"] != g["ignition"].shift()).cumsum()
    for _, seg in g[~g["ignition"]].groupby(run_id[~g["ignition"]]):
        lo, hi = max(seg.index[0] - 2, 0), min(seg.index[-1] + 2, len(g) - 1)
        yield seg, list(range(lo, hi + 1))


def refuel_events(gps: pd.DataFrame, noise: pd.Series, k: float = 4.0, min_pct: float = 3.0) -> pd.DataFrame:
    """Subidas de nivel con el camión detenido: las cargas que efectivamente entraron al tanque."""
    rows = []
    for truck_id, g in gps.sort_values("ts").groupby("truck_id"):
        g = g.reset_index(drop=True)
        tol = max(k * noise[truck_id], min_pct)
        for seg, idx in _off_runs(g):
            v = g.loc[idx]
            v = v[~v["trabado"]]
            lv, ts = v["fuel_level_pct"].to_numpy(), v["ts"].to_numpy()
            if len(lv) < 2:
                continue
            rises = [np.median(lv[j:j + 2]) - np.median(lv[max(0, j - 2):j]) for j in range(1, len(lv))]
            j = int(np.argmax(rises)) + 1
            if rises[j - 1] > tol:
                rows.append({
                    "truck_id": truck_id, "desde": ts[j - 1], "hasta": ts[j],
                    "lat": float(seg["lat"].median()), "lon": float(seg["lon"].median()),
                    "subida_pct": float(rises[j - 1]),
                    "saturado": bool(np.max(lv[j:j + 2]) >= 99.5),
                })
    ev = pd.DataFrame(rows)
    ev.insert(0, "event_id", [f"EV{i + 1:05d}" for i in range(len(ev))])
    return ev


def _presence(gps_by_truck: dict, truck_id, lat, lon, t0, t1) -> bool:
    g = gps_by_truck.get(truck_id)
    if g is None:
        return False
    w = g[(g["ts"] >= t0) & (g["ts"] <= t1)]
    return bool(len(w)) and bool((haversine_km(lat, lon, w["lat"].to_numpy(), w["lon"].to_numpy()) <= MATCH_RADIUS_KM).any())


def match_transactions(tx: pd.DataFrame, events: pd.DataFrame, gps: pd.DataFrame) -> pd.DataFrame:
    """Asigna cada transacción a una carga física.

    `vinculo` queda en:
      propia        -> carga del mismo camión de la tarjeta
      otro_camion   -> carga de otro camión de la flota (tarjeta compartida)
      sin_subida    -> el camión estuvo en la estación, pero el sensor no registró una subida clara
      sin_presencia -> ningún camión de la flota estuvo en la estación
    """
    gps_by_truck = {t: g.sort_values("ts") for t, g in gps.groupby("truck_id")}
    out = []
    for r in tx.sort_values("ts").itertuples():
        t0, t1 = r.ts - MAX_POSTING_DELAY, r.ts + CLOCK_TOLERANCE
        cand = events[(events["hasta"] >= t0) & (events["desde"] <= t1)]
        if len(cand):
            cand = cand[haversine_km(r.st_lat, r.st_lon, cand["lat"].to_numpy(), cand["lon"].to_numpy()) <= MATCH_RADIUS_KM]
        own = cand[cand["truck_id"] == r.truck_id]
        pick, vinculo = None, None
        if len(own):
            pick, vinculo = own.iloc[int(np.argmin(np.abs((own["hasta"] - r.ts).dt.total_seconds())))], "propia"
        elif len(cand):
            pick, vinculo = cand.iloc[int(np.argmin(np.abs((cand["hasta"] - r.ts).dt.total_seconds())))], "otro_camion"
        elif _presence(gps_by_truck, r.truck_id, r.st_lat, r.st_lon, t0, t1):
            vinculo = "sin_subida"
        else:
            vinculo = "sin_presencia"
        out.append({
            "tx_id": r.tx_id, "vinculo": vinculo,
            "event_id": None if pick is None else pick["event_id"],
            "camion_que_cargo": r.truck_id if pick is None else pick["truck_id"],
            "demora_h": None if pick is None else max((r.ts - pick["hasta"]).total_seconds() / 3600, 0.0),
        })
    return pd.DataFrame(out)
