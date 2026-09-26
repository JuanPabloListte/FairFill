"""Reglas explicables sobre cargas físicas emparejadas con transacciones.

Cada alerta responde una pregunta física: ¿algún camión de la flota estuvo en la estación?,
¿entró al tanque lo que se facturó?, ¿bajó el nivel sin que el camión se moviera más de lo
que explica el equipo de frío? Los umbrales salen de la dispersión observada en cada camión.

Además de alertas de fraude produce eventos informativos (cargas del equipo de frío, estaciones
que informan tarde, tarjetas compartidas) para que el operador vea por qué algo NO se alertó.
"""
import numpy as np
import pandas as pd

K_SIGMA = 4.0
MIN_SIPHON_L = 20.0
REEFER_SLACK = 1.25     # margen sobre el consumo teórico del equipo de frío
FRAUD_TYPES = {"tarjeta_lejos_del_camion", "litros_no_reflejados", "carga_repetida", "carga_mayor_al_consumo"}


def _mad_sd(x) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    return float(1.4826 * np.median(np.abs(x - np.median(x)))) if len(x) else np.nan


def outbound_intervals(trips: pd.DataFrame) -> dict:
    """Tramos de ida (cargado): el equipo de frío funciona. La base es el origen más frecuente."""
    base = trips["origen"].mode().iat[0]
    out = trips[trips["origen"] == base]
    return {t: list(zip(g["salida"], g["llegada"])) for t, g in out.groupby("truck_id")}


def _overlap_h(intervals, t0, t1) -> float:
    return sum(max((min(b, t1) - max(a, t0)).total_seconds(), 0) for a, b in intervals) / 3600


def calibrate(tx: pd.DataFrame, events: pd.DataFrame, trucks: pd.DataFrame, noise: pd.Series) -> pd.DataFrame:
    """Ganancia del sensor y tolerancia en litros para cada camión.

    Usa cargas con una sola transacción propia y sin saturación: la mediana del cociente
    subida/litros estima la ganancia; la dispersión robusta del residuo, la tolerancia.
    """
    tanks = trucks.set_index("truck_id")["tanque_l"]
    one = tx[tx["vinculo"] == "propia"].groupby("event_id").filter(lambda g: len(g) == 1)
    pairs = one.merge(events[["event_id", "subida_pct", "saturado"]], on="event_id")
    pairs = pairs[~pairs["saturado"]]
    pairs["subida_l_bruta"] = pairs["subida_pct"] / 100 * pairs["truck_id"].map(tanks)
    rows = []
    for truck_id, tank in tanks.items():
        p = pairs[pairs["truck_id"] == truck_id]
        gain = float(np.median(p["subida_l_bruta"] / p["litros"])) if len(p) >= 5 else 1.0
        resid_sd = _mad_sd(p["litros"] - p["subida_l_bruta"] / gain) if len(p) >= 5 else np.nan
        sensor_floor = K_SIGMA * noise.get(truck_id, 2.0) * np.sqrt(2) * tank / 100
        rows.append({"truck_id": truck_id, "ganancia": gain, "pares": len(p),
                     "tolerancia_l": float(np.nanmax([K_SIGMA * resid_sd, sensor_floor]))})
    return pd.DataFrame(rows).set_index("truck_id")


def transaction_alerts(tx: pd.DataFrame, events: pd.DataFrame, trucks: pd.DataFrame,
                       calib: pd.DataFrame, trips: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    tk = trucks.set_index("truck_id")
    ev = events.set_index("event_id")
    loaded = outbound_intervals(trips)
    alerts, info = [], []

    def add(bucket, r, tipo, liters, text, truck_id=None):
        bucket.append({"truck_id": truck_id or r.truck_id, "ts": r.ts, "tx_id": r.tx_id, "tipo": tipo,
                       "litros_en_riesgo": round(float(liters), 1),
                       "ars_en_riesgo": round(float(liters) * r.precio_unitario), "explicacion": text})

    for r in tx[tx["vinculo"] == "sin_presencia"].itertuples():
        add(alerts, r, "tarjeta_lejos_del_camion", r.litros,
            "ningún camión de la flota pasó a menos de 3 km de la estación en las 14 h previas")
    for r in tx[tx["vinculo"] == "sin_subida"].itertuples():
        tol = calib.at[r.truck_id, "tolerancia_l"]
        if r.litros > tol:
            add(alerts, r, "litros_no_reflejados", r.litros,
                f"el camión estuvo en la estación pero el sensor no registró la carga de {r.litros:.0f} L")
        else:
            add(info, r, "carga_chica_no_verificable", 0, f"{r.litros:.0f} L, por debajo de lo que el sensor distingue")
    for r in tx[tx["vinculo"] == "otro_camion"].itertuples():
        add(alerts, r, "tarjeta_compartida", 0,
            f"la tarjeta de {r.truck_id} se usó para cargar {r.camion_que_cargo}", truck_id=r.truck_id)
    for r in tx[(tx["demora_h"] > 1)].itertuples():
        add(info, r, "posteo_demorado", 0, f"la estación informó la transacción {r.demora_h:.1f} h después de la carga")

    # Cargas físicas con sus transacciones, en orden cronológico (el frío necesita su historia).
    last_reefer, summary = {}, []
    matched = tx.dropna(subset=["event_id"])
    for event_id in ev.loc[matched["event_id"].unique()].sort_values("hasta").index:
        e = ev.loc[event_id]
        truck_id = e["truck_id"]
        tank, gain, tol = tk.at[truck_id, "tanque_l"], calib.at[truck_id, "ganancia"], calib.at[truck_id, "tolerancia_l"]
        rise_l = e["subida_pct"] / 100 * tank / gain
        group = matched[matched["event_id"] == event_id].sort_values("litros", ascending=False)
        principal, extras = _row(group.iloc[0]), group.iloc[1:].sort_values("ts")

        fuel_txs = [principal]
        for x in extras.itertuples():
            if tk.at[truck_id, "tanque_frio_l"] > 0:
                since = last_reefer.get(truck_id, tx["ts"].min())
                allowance = tk.at[truck_id, "consumo_frio_lh"] * _overlap_h(loaded.get(truck_id, []), since, e["hasta"])
                allowance = min(allowance * REEFER_SLACK + 10, tk.at[truck_id, "tanque_frio_l"])
                if x.litros <= allowance:
                    last_reefer[truck_id] = e["hasta"]
                    add(info, x, "carga_equipo_frio", 0,
                        f"{x.litros:.0f} L al equipo de frío (consumo esperado hasta {allowance:.0f} L)", truck_id)
                    continue
            fuel_txs.append(x)

        billed = sum(float(t.litros) for t in fuel_txs)
        unexplained = billed - rise_l
        summary.append({
            "event_id": event_id, "truck_id": truck_id, "billed_l": billed, "rise_l": rise_l,
            "verificado_sensor": bool(unexplained <= tol or not e["saturado"]),
            "tx_principal": principal.tx_id, "tx_extras": [t.tx_id for t in fuel_txs[1:]],
            "ts_principal": principal.ts, "precio": principal.precio_unitario,
        })
        if unexplained <= tol:
            continue
        if e["saturado"]:
            # Con el sensor en su tope la subida medida es un mínimo: no alcanza para acusar.
            add(info, principal, "no_verificable_sensor_saturado", 0,
                f"faltan {unexplained:.0f} L pero el sensor llegó a su tope", truck_id)
            continue
        if len(fuel_txs) > 1:
            for x in fuel_txs[1:]:
                add(alerts, x, "carga_repetida", min(float(x.litros), unexplained),
                    f"carga adicional de {x.litros:.0f} L a los {(x.ts - principal.ts).total_seconds() / 60:.0f} min; "
                    f"entre todas faltan {unexplained:.0f} L en el tanque", truck_id)
        else:
            add(alerts, principal, "litros_no_reflejados", unexplained,
                f"se facturaron {principal.litros:.0f} L pero el tanque subió {rise_l:.0f} L", truck_id)

    cols = ["truck_id", "ts", "tx_id", "tipo", "litros_en_riesgo", "ars_en_riesgo", "explicacion"]
    return pd.DataFrame(alerts, columns=cols), pd.DataFrame(info, columns=cols), pd.DataFrame(summary)


def _row(s: pd.Series):
    """Convierte una fila de pandas en un objeto con atributos, como los de itertuples()."""
    return pd.DataFrame([s]).itertuples().__next__()


Z_SIPHON = 4.5   # más alto que K_SIGMA: se prueban muchos cortes por parada


def siphon_alerts(gps: pd.DataFrame, trucks: pd.DataFrame, noise: pd.Series, calib: pd.DataFrame,
                  trips: pd.DataFrame, median_price: float) -> pd.DataFrame:
    """Caídas de nivel con el motor apagado, con un test de dos medias en cada parada.

    Para cada corte posible compara el promedio de todas las lecturas anteriores con el de las
    posteriores. Con paradas largas el promedio de muchas lecturas reduce el ruido y deja ver
    robos chicos o graduales que una comparación entre pocas lecturas confunde con ruido.
    Antes de comparar descuenta el consumo del equipo de frío si toma del tanque principal.
    """
    tk = trucks.set_index("truck_id")
    loaded = outbound_intervals(trips)
    rows = []
    for truck_id, g in gps.sort_values("ts").groupby("truck_id"):
        g = g.reset_index(drop=True)
        tank, gain, sd = tk.at[truck_id, "tanque_l"], calib.at[truck_id, "ganancia"], noise[truck_id]
        to_l = tank / 100 / gain
        reefer_lph = tk.at[truck_id, "consumo_frio_lh"] if tk.at[truck_id, "equipo_frio"] == "tanque_principal" else 0.0
        run_id = (g["ignition"] != g["ignition"].shift()).cumsum()
        for _, seg in g[~g["ignition"]].groupby(run_id[~g["ignition"]]):
            start = seg.index[0]
            idx = ([start - 1] if start > 0 else []) + list(seg.index)
            v = g.loc[idx]
            v = v[~v["trabado"]]
            n = len(v)
            if n < 2:
                continue
            lv, ts = v["fuel_level_pct"].to_numpy(), v["ts"].to_numpy()
            t0, t1 = pd.Timestamp(ts[0]), pd.Timestamp(ts[-1])
            hours = (v["ts"] - t0).dt.total_seconds().to_numpy() / 3600
            dur_h = hours[-1] or 1.0
            reefer_pct_h = reefer_lph * _overlap_h(loaded.get(truck_id, []), t0, t1) / dur_h / to_l if reefer_lph else 0.0
            adj = lv + reefer_pct_h * hours

            csum = np.cumsum(adj)
            j = np.arange(1, n)
            before = csum[j - 1] / j
            after = (csum[-1] - csum[j - 1]) / (n - j)
            drop = before - after
            z = drop / (sd * np.sqrt(1 / j + 1 / (n - j)))
            best = int(np.argmax(z))
            liters = drop[best] * to_l
            if z[best] < Z_SIPHON or liters < MIN_SIPHON_L:
                continue
            cut = best + 1
            extra = f"; descontados {reefer_pct_h * dur_h * to_l:.0f} L del equipo de frío" if reefer_pct_h else ""
            rows.append({
                "truck_id": truck_id, "ts": pd.Timestamp(ts[cut]), "tx_id": None, "tipo": "caida_con_motor_apagado",
                "litros_en_riesgo": round(float(liters), 1), "ars_en_riesgo": round(float(liters) * median_price),
                "explicacion": f"el nivel promedio bajó {liters:.0f} L con el motor apagado alrededor de "
                               f"{pd.Timestamp(ts[cut]):%d/%m %H:%M} (z = {z[best]:.1f}){extra}",
                "desde": pd.Timestamp(ts[cut - 1]), "hasta": pd.Timestamp(ts[cut]),
            })
    return pd.DataFrame(rows)
