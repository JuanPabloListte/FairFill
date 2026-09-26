"""Verificación por consumo (tanque lleno a tanque lleno), para cargas que el sensor no puede verificar.

Cuando el sensor llega a su tope, la subida medida es solo un mínimo. Pero si el tanque quedó
lleno en la carga anterior y en esta, lo facturado debería igualar lo consumido en el medio:

    litros ≈ a·km + b·(t·km) + c·(horas de frío tomando del tanque principal)

Se ajusta una regresión robusta (Huber) por camión: robusta porque los datos de ajuste incluyen
las propias anomalías que se buscan.
"""
import numpy as np
import pandas as pd
from sklearn.linear_model import HuberRegressor

from .rules import K_SIGMA, _mad_sd, _overlap_h, outbound_intervals

MIN_PAIRS = 8


def _ton_km(trips: pd.DataFrame, t0, t1) -> float:
    """t·km recorridos entre t0 y t1, prorrateando los viajes que quedan cortados."""
    total = 0.0
    for r in trips.itertuples():
        dur = (r.llegada - r.salida).total_seconds()
        ov = (min(r.llegada, t1) - max(r.salida, t0)).total_seconds()
        if dur > 0 and ov > 0:
            total += r.carga_kg / 1000 * r.km_odometro * ov / dur
    return total


def fill_to_fill_alerts(summary: pd.DataFrame, events: pd.DataFrame, gps: pd.DataFrame, trips: pd.DataFrame,
                        trucks: pd.DataFrame, siphon: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Devuelve (alertas, tabla de ajuste por camión)."""
    tk = trucks.set_index("truck_id")
    loaded = outbound_intervals(trips)
    odo = gps[["truck_id", "ts", "odometer_km"]].sort_values("ts")
    ev = pd.merge_asof(events.sort_values("hasta"), odo.rename(columns={"ts": "ts_odo"}),
                       left_on="hasta", right_on="ts_odo", by="truck_id", direction="nearest")
    ev = ev.merge(summary, on=["event_id", "truck_id"], how="left")

    alerts, fits = [], []
    for truck_id, g in ev.groupby("truck_id"):
        g = g.sort_values("hasta").reset_index(drop=True)
        prev = g.shift()
        ok = g["saturado"] & prev["saturado"].fillna(False).astype(bool) & g["billed_l"].notna()
        pairs = g[ok].copy()
        if len(pairs) < MIN_PAIRS:
            fits.append({"truck_id": truck_id, "pares": len(pairs)})
            continue
        t_trips = trips[trips["truck_id"] == truck_id]
        reefer_main = tk.at[truck_id, "equipo_frio"] == "tanque_principal"
        s_truck = siphon[siphon["truck_id"] == truck_id] if len(siphon) else siphon
        feats = []
        for i, r in pairs.iterrows():
            t0, t1 = prev.at[i, "hasta"], r["desde"]
            stolen = float(s_truck[(s_truck["ts"] > t0) & (s_truck["ts"] <= t1)]["litros_en_riesgo"].sum()) if len(s_truck) else 0.0
            feats.append({
                "km": r["odometer_km"] - prev.at[i, "odometer_km"],
                "tkm": _ton_km(t_trips, t0, t1),
                "frio_h": _overlap_h(loaded.get(truck_id, []), t0, t1) if reefer_main else 0.0,
                # El combustible robado (ya alertado) se repone en la carga siguiente: no es sobrefacturación.
                "robado_l": stolen,
            })
        X = pd.DataFrame(feats, index=pairs.index)
        cols = ["km", "tkm"] + (["frio_h"] if reefer_main else [])
        scale = X[cols].std().replace(0, 1)
        y = pairs["billed_l"] - X["robado_l"]
        model = HuberRegressor(fit_intercept=False, max_iter=2000).fit(X[cols] / scale, y)
        pred = model.predict(X[cols] / scale)
        resid = y - pred
        sd = _mad_sd(resid)
        tol = max(K_SIGMA * sd, 0.03 * tk.at[truck_id, "tanque_l"])
        coefs = dict(zip(cols, model.coef_ / scale.to_numpy()))
        fits.append({"truck_id": truck_id, "pares": len(pairs), "l_100km_vacio": round(coefs["km"] * 100, 1),
                     "l_100tkm": round(coefs["tkm"] * 100, 2), "frio_lh": round(coefs.get("frio_h", 0), 2),
                     "residuo_sd_l": round(sd, 1), "tolerancia_l": round(tol, 1)})
        # Solo se alerta lo que el sensor no pudo verificar: el consumo es la segunda opinión.
        for i in pairs.index[(resid > tol) & ~pairs["verificado_sensor"].astype(bool)]:
            r = pairs.loc[i]
            # Sospechosa la carga adicional si la hubo; si no, la principal.
            target_tx = r["tx_extras"][-1] if isinstance(r["tx_extras"], list) and r["tx_extras"] else r["tx_principal"]
            alerts.append({
                "truck_id": truck_id, "ts": r["ts_principal"], "tx_id": target_tx,
                "tipo": "carga_mayor_al_consumo", "litros_en_riesgo": round(float(resid[i]), 1),
                "ars_en_riesgo": round(float(resid[i]) * r["precio"]),
                "explicacion": f"tanque lleno a tanque lleno: se facturaron {r['billed_l']:.0f} L y por "
                               f"{X.at[i, 'km']:.0f} km recorridos se esperaban {pred[pairs.index.get_loc(i)] + X.at[i, 'robado_l']:.0f} L",
            })
    cols_out = ["truck_id", "ts", "tx_id", "tipo", "litros_en_riesgo", "ars_en_riesgo", "explicacion"]
    return pd.DataFrame(alerts, columns=cols_out), pd.DataFrame(fits)
