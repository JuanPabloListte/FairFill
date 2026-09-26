"""Isolation Forest sobre las transacciones: detector no supervisado, sin reglas escritas a mano.

Sirve para comparar: ¿un modelo genérico encuentra lo mismo que las reglas físicas?
Incluye a propósito la diferencia de odómetro, que es un problema de calidad de dato y no un
fraude, para ver si el modelo confunde una cosa con la otra.
"""
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

FEATURES = {
    "dist_gps": lambda f: np.log1p(f["dist_gps_km"]),
    "no_reflejado": lambda f: f["litros_no_reflejados"] / f["tanque_l"],
    "exceso_capacidad": lambda f: f["exceso_capacidad_l"] / f["tanque_l"],
    "min_desde_anterior": lambda f: np.log1p(f["min_desde_carga_anterior"].fillna(10_000)),
    "dif_odometro": lambda f: np.log1p(f["dif_odometro_km"].abs()),
}


def feature_matrix(f: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({name: fn(f) for name, fn in FEATURES.items()})


def score(f: pd.DataFrame, seed: int = 0) -> np.ndarray:
    """Puntaje de anomalía: más alto = más raro."""
    X = feature_matrix(f)
    model = IsolationForest(n_estimators=300, random_state=seed).fit(X)
    return -model.score_samples(X)


def top_alerts(f: pd.DataFrame, scores: np.ndarray, budget: int) -> pd.DataFrame:
    """Las `budget` transacciones más raras, con la feature que más se aparta como explicación."""
    X = feature_matrix(f)
    z = (X - X.median()) / (1.4826 * (X - X.median()).abs().median() + 1e-9)
    order = np.argsort(-scores)[:budget]
    return pd.DataFrame([{
        "fuente": "isolation_forest", "tipo": "transaccion_atipica", "truck_id": f.at[i, "truck_id"],
        "ts": f.at[i, "ts"], "tx_id": f.at[i, "tx_id"], "puntaje": round(float(scores[i]), 3),
        "litros_en_riesgo": round(float(max(f.at[i, "litros_no_reflejados"], 0)), 1),
        "explicacion": f"más atípico en: {z.loc[i].abs().idxmax()}",
    } for i in order])
