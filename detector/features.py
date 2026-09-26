"""Features para detectar anomalías de combustible, calculadas solo con datos que la empresa tiene.

Nunca lee `_ground_truth/`: eso queda reservado para `evaluate.py`.
"""
from pathlib import Path

import numpy as np
import pandas as pd

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


def load_company_data(run_dir: Path) -> dict[str, pd.DataFrame]:
    return {
        "tx": pd.read_csv(run_dir / "fuel_transactions.csv", parse_dates=["ts"]),
        "gps": pd.read_csv(run_dir / "gps.csv.gz", parse_dates=["ts"]),
        "trucks": pd.read_csv(run_dir / "trucks.csv", keep_default_na=False),
        "trips": pd.read_csv(run_dir / "trips.csv", parse_dates=["salida", "llegada"]),
        "stations": pd.read_csv(run_dir / "stations.csv", usecols=["idempresa", "direccion", "lat", "lon"]),
    }


def estimate_sensor_noise_pct(gps: pd.DataFrame) -> float:
    """Desvío del sensor de nivel, estimado con pares de lecturas consecutivas con el motor apagado.

    Con el camión detenido el nivel real no cambia, así que la diferencia entre lecturas es ruido.
    Se usa la MAD en lugar del desvío estándar para que los robos (saltos grandes) no inflen la estimación.
    """
    g = gps.sort_values(["truck_id", "ts"])
    same = (g["truck_id"] == g["truck_id"].shift()) & ~g["ignition"] & ~g["ignition"].shift(fill_value=True)
    diff = g["fuel_level_pct"].diff()[same].dropna()
    mad = np.median(np.abs(diff - np.median(diff)))
    return float(1.4826 * mad / np.sqrt(2))


def transaction_features(data: dict, repeat_window_min: float = 90.0, max_dist_km: float = 20.0) -> pd.DataFrame:
    tx, gps, trucks, stations = data["tx"], data["gps"], data["trucks"], data["stations"]
    stations = stations.rename(columns={"idempresa": "station_idempresa", "lat": "st_lat", "lon": "st_lon"})
    df = (
        tx.merge(stations, on=["station_idempresa", "direccion"], how="left")
        .merge(trucks[["truck_id", "tanque_l"]], on="truck_id")
        .sort_values("ts")
    )
    g = gps.sort_values("ts")
    tol = pd.Timedelta(hours=2)

    near = pd.merge_asof(
        df[["tx_id", "ts", "truck_id"]],
        g[["truck_id", "ts", "lat", "lon", "odometer_km"]].rename(columns={"ts": "gps_ts"}),
        left_on="ts", right_on="gps_ts", by="truck_id", direction="nearest",
    )
    before = pd.merge_asof(
        df[["tx_id", "ts", "truck_id"]],
        g[["truck_id", "ts", "fuel_level_pct"]].rename(columns={"ts": "ts_antes", "fuel_level_pct": "nivel_antes_pct"}),
        left_on="ts", right_on="ts_antes", by="truck_id", direction="backward", tolerance=tol,
    )
    after = pd.merge_asof(
        df[["tx_id", "ts", "truck_id"]],
        g[["truck_id", "ts", "fuel_level_pct"]].rename(columns={"ts": "ts_despues", "fuel_level_pct": "nivel_despues_pct"}),
        left_on="ts", right_on="ts_despues", by="truck_id", direction="forward", tolerance=tol,
    )
    df = (
        df.merge(near.drop(columns=["ts", "truck_id"]), on="tx_id")
        .merge(before[["tx_id", "nivel_antes_pct"]], on="tx_id")
        .merge(after[["tx_id", "nivel_despues_pct"]], on="tx_id")
    )

    # ¿Dónde estaba el camión cuando se usó la tarjeta?
    df["dist_gps_km"] = haversine_km(df["st_lat"], df["st_lon"], df["lat"], df["lon"])
    df["desfase_gps_min"] = (df["ts"] - df["gps_ts"]).abs().dt.total_seconds() / 60

    # ¿Entró al tanque lo que se facturó?
    df["capacidad_libre_l"] = df["tanque_l"] * (1 - df["nivel_antes_pct"] / 100)
    df["exceso_capacidad_l"] = df["litros"] - df["capacidad_libre_l"]
    df["subida_sensor_l"] = df["tanque_l"] * (df["nivel_despues_pct"] - df["nivel_antes_pct"]) / 100
    df["litros_no_reflejados"] = df["litros"] - df["subida_sensor_l"]

    # Patrón temporal por tarjeta.
    df = df.sort_values(["card_id", "ts"])
    prev = df.groupby("card_id")
    df["min_desde_carga_anterior"] = (df["ts"] - prev["ts"].shift()).dt.total_seconds() / 60
    df["misma_estacion_anterior"] = (
        (df["station_idempresa"] == prev["station_idempresa"].shift())
        & (df["direccion"] == prev["direccion"].shift())
    )

    # Cargas de la misma tarjeta separadas por pocos minutos comparten las lecturas del sensor
    # (la de antes es previa a la primera y la de después, posterior a la última), así que se
    # evalúan juntas: litros facturados del grupo contra lo que subió el tanque en total.
    # Solo se agrupan cargas hechas donde estaba el camión: una transacción lejana no pudo
    # haber entrado a este tanque y, si se agrupara, haría parecer sospechosa a la carga real.
    near = ~(df["dist_gps_km"] > max_dist_km)
    sub = df[near]
    gap = sub.groupby("card_id")["ts"].diff().dt.total_seconds() / 60
    df["min_desde_carga_cercana_anterior"] = gap
    df["grupo_carga"] = -np.arange(1, len(df) + 1)  # las lejanas quedan solas
    df.loc[near, "grupo_carga"] = (~(gap <= repeat_window_min)).cumsum()
    grp = df.groupby("grupo_carga")
    subida_grupo = df["tanque_l"] * (grp["nivel_despues_pct"].transform("last") - grp["nivel_antes_pct"].transform("first")) / 100
    df["litros_no_reflejados_grupo"] = grp["litros"].transform("sum") - subida_grupo

    # Calidad del dato: odómetro tipeado por el chofer contra el del GPS.
    df["dif_odometro_km"] = df["odometro_informado"] - df["odometer_km"]
    return df.sort_values("ts").reset_index(drop=True)


def parking_drop_events(gps: pd.DataFrame, trucks: pd.DataFrame, window: int = 3) -> pd.DataFrame:
    """Mayor caída de nivel dentro de cada período con el motor apagado.

    Compara la mediana de `window` lecturas antes y después de cada punto: la mediana amortigua
    el ruido del sensor sin perder un escalón brusco, que es la firma de un robo.
    Incluye la última lectura en movimiento previa como referencia, por si el robo ocurre
    antes del primer ping detenido.
    """
    tanks = trucks.set_index("truck_id")["tanque_l"]
    rows = []
    for truck_id, g in gps.sort_values("ts").groupby("truck_id"):
        g = g.reset_index(drop=True)
        run_id = (g["ignition"] != g["ignition"].shift()).cumsum()
        for _, seg in g[~g["ignition"]].groupby(run_id[~g["ignition"]]):
            start = seg.index[0]
            idx = ([start - 1] if start > 0 else []) + list(seg.index)
            if len(idx) < 2:
                continue
            lv = g.loc[idx, "fuel_level_pct"].to_numpy()
            ts = g.loc[idx, "ts"].to_numpy()
            best_drop, best_j = -np.inf, None
            for j in range(1, len(lv)):
                drop = np.median(lv[max(0, j - window):j]) - np.median(lv[j:j + window])
                if drop > best_drop:
                    best_drop, best_j = drop, j
            # Las medianas detectan bien el tamaño de la caída pero pueden desplazarla una lectura:
            # se ubica el momento exacto buscando el salto crudo mayor en la zona.
            lo, hi = max(1, best_j - window + 1), min(len(lv), best_j + window)
            best_j = lo + int(np.argmax(lv[lo - 1:hi - 1] - lv[lo:hi]))
            rows.append({
                "truck_id": truck_id, "desde": ts[best_j - 1], "hasta": ts[best_j],
                "inicio_parada": ts[0], "fin_parada": ts[-1], "lecturas": len(lv),
                "lat": g.loc[idx[best_j], "lat"], "lon": g.loc[idx[best_j], "lon"],
                "caida_pct": best_drop, "caida_l": best_drop * tanks[truck_id] / 100,
                "tanque_l": tanks[truck_id],
            })
    return pd.DataFrame(rows)
