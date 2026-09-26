"""python -m detector [--run output/base] [--no-eval]"""
import argparse
import json
from pathlib import Path

import pandas as pd

from . import consumption, evaluate, model, rules
from .features import load_company_data, transaction_features
from .matching import mark_stuck, match_transactions, refuel_events, sensor_noise_by_truck

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser(description="Detector de anomalías de combustible")
    ap.add_argument("--run", default=str(ROOT / "output" / "base"))
    ap.add_argument("--no-eval", action="store_true", help="no leer la verdad del simulador")
    args = ap.parse_args()
    run_dir = Path(args.run)

    data = load_company_data(run_dir)
    gps, trucks, trips = data["gps"], data["trucks"], data["trips"]
    gps["trabado"] = mark_stuck(gps)
    noise = sensor_noise_by_truck(gps)
    f = transaction_features(data)
    events = refuel_events(gps, noise)
    tx = f.merge(match_transactions(f, events, gps), on="tx_id")
    calib = rules.calibrate(tx, events, trucks, noise)
    print(f"{len(tx)} transacciones | {len(events)} cargas físicas en el sensor | "
          f"{int(gps['trabado'].sum())} lecturas trabadas | ruido por camión {noise.min():.1f}–{noise.max():.1f}%")
    print("vínculos:", tx["vinculo"].value_counts().to_dict())

    tx_alerts, info, summary = rules.transaction_alerts(tx, events, trucks, calib, trips)
    siphon = rules.siphon_alerts(gps, trucks, noise, calib, trips, float(tx["precio_unitario"].median()))
    cons_alerts, cons_fit = consumption.fill_to_fill_alerts(summary, events, gps, trips, trucks, siphon)
    tx_alerts = pd.concat([tx_alerts, cons_alerts], ignore_index=True)
    cons_fit.to_csv(run_dir / "consumption_model.csv", index=False)
    fraud_tx = tx_alerts[tx_alerts["tipo"].isin(rules.FRAUD_TYPES)]
    scores = model.score(f)
    if_alerts = model.top_alerts(f, scores, budget=max(len(fraud_tx), 1))

    alerts = pd.concat([tx_alerts, siphon], ignore_index=True).sort_values("ts")
    alerts.insert(0, "alert_id", [f"AL{i + 1:05d}" for i in range(len(alerts))])
    alerts.to_csv(run_dir / "alerts.csv", index=False)
    info.sort_values("ts").to_csv(run_dir / "info_events.csv", index=False)
    if_alerts.to_csv(run_dir / "alerts_isolation_forest.csv", index=False)
    tx.to_csv(run_dir / "tx_features.csv", index=False)
    events.to_csv(run_dir / "refuel_events.csv", index=False)
    calib.to_csv(run_dir / "sensor_calibration.csv")
    print(f"{len(alerts)} alertas, ${alerts['ars_en_riesgo'].sum():,.0f} en riesgo -> {run_dir / 'alerts.csv'}")
    print(alerts["tipo"].value_counts().to_string())
    print("informativos (no alertados):", info["tipo"].value_counts().to_dict())

    if args.no_eval:
        return
    truth, legit = evaluate.load_truth(run_dir)
    shared_truth = set(legit.loc[legit["tipo"] == "tarjeta_compartida", "tx_id"].dropna())
    shared_alerts = set(tx_alerts.loc[tx_alerts["tipo"] == "tarjeta_compartida", "tx_id"])
    report = {
        "reglas_transacciones_fraude": evaluate.evaluate_transactions(fraud_tx, truth, legit),
        "reglas_tarjeta_compartida": f"{len(shared_alerts & shared_truth)}/{len(shared_truth)} reales identificadas, "
                                     f"{len(shared_alerts - shared_truth)} identificadas de más",
        "isolation_forest_mismo_presupuesto": evaluate.evaluate_transactions(if_alerts, truth, legit),
        "isolation_forest_average_precision": evaluate.evaluate_scores(f, scores, truth),
        "reglas_sifonado": evaluate.evaluate_siphon(siphon, truth, legit),
    }
    (run_dir / "detector_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
