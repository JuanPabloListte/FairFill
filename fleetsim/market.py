"""Precios "verdaderos" de combustible a lo largo de la simulación y tarifas de peaje."""
from datetime import datetime

import numpy as np
import pandas as pd


class PriceModel:
    """Precio por estación y día.

    precio = precio_base * factor_estacion * índice_bandera(día)

    El índice sube en escalones: la bandera líder aumenta cada ~N días y las demás la siguen
    con unos días de demora y una magnitud parecida. Los niveles quedan anclados a los precios
    vigentes al momento de descargar el dataset (día 0 de la simulación = precios actuales).
    """

    def __init__(self, stations: pd.DataFrame, fuel_cfg: dict, start: datetime, days: int, rng: np.random.Generator):
        self.start = start
        horizon = days + 30  # margen para viajes que terminan después del último día
        leader = fuel_cfg["leader_brand"]
        brands = sorted(set(stations["bandera"]) | {leader})

        leader_events = []
        t = rng.uniform(*fuel_cfg["increase_every_days"]) / 2
        while t < horizon:
            leader_events.append((int(t), rng.uniform(*fuel_cfg["increase_pct"])))
            t += rng.uniform(*fuel_cfg["increase_every_days"])

        self.events = {leader: leader_events}
        for b in brands:
            if b == leader:
                continue
            self.events[b] = [
                (int(day + rng.uniform(*fuel_cfg["follower_lag_days"])), max(0.0, pct * rng.normal(1.0, 0.2)))
                for day, pct in leader_events
            ]

        self.index = {}
        for b, evs in self.events.items():
            steps = np.zeros(horizon + 1)
            for day, pct in evs:
                if day <= horizon:
                    steps[day] += np.log1p(pct)
            self.index[b] = np.exp(np.cumsum(steps))

    def price(self, station, when: datetime) -> float:
        day = min(max((when - self.start).days, 0), len(self.index[station.bandera]) - 1)
        return float(station.precio_base * station.factor_estacion * self.index[station.bandera][day])

    def events_frame(self) -> pd.DataFrame:
        rows = [
            {"bandera": b, "fecha": self.start + pd.Timedelta(days=d), "aumento_pct": round(p * 100, 2)}
            for b, evs in self.events.items() for d, p in evs
        ]
        return pd.DataFrame(rows).sort_values(["fecha", "bandera"])


class TollTariff:
    def __init__(self, tolls_cfg: dict):
        self.tariffs = tolls_cfg["tariffs"]
        self.peak_hours = tolls_cfg["peak_hours"]
        self.weekdays_only = tolls_cfg["peak_weekdays_only"]

    def is_peak(self, when: datetime) -> bool:
        if self.weekdays_only and when.weekday() >= 5:
            return False
        return any(a <= when.hour < b for a, b in self.peak_hours)

    def amount(self, category: str, when: datetime) -> tuple[float, bool]:
        peak = self.is_peak(when)
        off, on = self.tariffs[category]
        return float(on if peak else off), peak
