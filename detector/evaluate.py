"""Evaluación contra la verdad del simulador. Es el único módulo que lee `_ground_truth/`."""
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score

TX_TYPES = {"sobrecarga_tanque", "carga_duplicada", "tarjeta_fuera_de_ruta"}
SLACK = pd.Timedelta(minutes=5)


def load_truth(run_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    gt = run_dir / "_ground_truth"
    anomalies = pd.read_csv(gt / "anomalies.csv", parse_dates=["ts"])
    if "ts_fin" in anomalies:
        anomalies["ts_fin"] = pd.to_datetime(anomalies["ts_fin"]).fillna(anomalies["ts"])
    else:
        anomalies["ts_fin"] = anomalies["ts"]
    legit_path = gt / "legit_events.csv"
    legit = pd.read_csv(legit_path, parse_dates=["ts"]) if legit_path.exists() else pd.DataFrame(columns=["tipo", "truck_id", "ts"])
    if "hasta" in legit:
        legit["hasta"] = pd.to_datetime(legit["hasta"]).fillna(legit["ts"])
    else:
        legit["hasta"] = legit["ts"]
    if "tx_id" not in legit:
        legit["tx_id"] = np.nan
    return anomalies, legit


def _fp_causes_tx(fp_ids: set, legit: pd.DataFrame) -> dict:
    by_tx = legit.dropna(subset=["tx_id"]).groupby("tx_id")["tipo"].agg(lambda s: "+".join(sorted(set(s))))
    causes = pd.Series([by_tx.get(t, "sin_causa_legitima") for t in fp_ids], dtype=object)
    return causes.value_counts().to_dict()


def evaluate_transactions(alerts: pd.DataFrame, truth: pd.DataFrame, legit: pd.DataFrame) -> dict:
    t = truth[truth["tipo"].isin(TX_TYPES)]
    flagged = set(alerts["tx_id"].dropna())
    tp = t[t["tx_id"].isin(flagged)]
    fp = flagged - set(t["tx_id"])
    return {
        "alertas": len(flagged), "verdaderas": len(tp), "anomalias_reales": len(t),
        "precision": round(len(tp) / len(flagged), 3) if flagged else None,
        "recall": round(len(tp) / len(t), 3) if len(t) else None,
        "recall_por_tipo": {tipo: f"{int(g['tx_id'].isin(flagged).sum())}/{len(g)}" for tipo, g in t.groupby("tipo")},
        "falsos_positivos_por_causa": _fp_causes_tx(fp, legit),
    }


def evaluate_scores(f: pd.DataFrame, scores: np.ndarray, truth: pd.DataFrame) -> float:
    y = f["tx_id"].isin(truth.loc[truth["tipo"].isin(TX_TYPES), "tx_id"]).astype(int)
    return round(float(average_precision_score(y, scores)), 3)


def _overlaps(df: pd.DataFrame, truck_id, desde, hasta, start_col, end_col) -> pd.DataFrame:
    return df[(df["truck_id"] == truck_id) & (df[start_col] <= hasta + SLACK) & (df[end_col] >= desde - SLACK)]


def evaluate_siphon(alerts: pd.DataFrame, truth: pd.DataFrame, legit: pd.DataFrame) -> dict:
    t = truth[truth["tipo"] == "sifonado"].reset_index(drop=True)
    matched, tp_alerts, causes = set(), 0, []
    for a in alerts.itertuples():
        hit = _overlaps(t, a.truck_id, a.desde, a.hasta, "ts", "ts_fin")
        if not hit.empty:
            tp_alerts += 1
            matched.update(hit.index)
            continue
        why = _overlaps(legit, a.truck_id, a.desde, a.hasta, "ts", "hasta")["tipo"]
        causes.append("+".join(sorted(set(why))) if len(why) else "sin_causa_legitima")
    missed = t.drop(index=list(matched))
    return {
        "alertas": len(alerts), "verdaderas": tp_alerts, "robos_reales": len(t),
        "precision": round(tp_alerts / len(alerts), 3) if len(alerts) else None,
        "recall": round(len(matched) / len(t), 3) if len(t) else None,
        "falsos_positivos_por_causa": pd.Series(causes, dtype=object).value_counts().to_dict(),
        "no_detectados": [f"{r.litros:.0f} L{' gradual' if r.ts_fin > r.ts else ''}" for r in missed.itertuples()],
    }
