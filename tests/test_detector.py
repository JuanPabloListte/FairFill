import numpy as np
import pandas as pd

from detector.features import estimate_sensor_noise_pct, parking_drop_events


def _parked(levels, truck_id="CAM-001", start="2026-06-01"):
    ts = pd.date_range(start, periods=len(levels), freq="h")
    return pd.DataFrame({
        "truck_id": truck_id, "ts": ts, "lat": -31.4, "lon": -64.2,
        "ignition": False, "fuel_level_pct": levels,
    })


def test_sensor_noise_is_recovered_despite_a_theft():
    rng = np.random.default_rng(0)
    levels = 60 + rng.normal(0, 1.5, 2000)
    levels[1000:] -= 20  # un robo no debería inflar la estimación
    est = estimate_sensor_noise_pct(_parked(levels))
    assert abs(est - 1.5) < 0.15


def test_parking_drop_is_located_at_the_exact_step():
    levels = [60.2, 59.8, 60.1, 60.0, 40.1, 39.9, 40.2]
    gps = _parked(levels)
    trucks = pd.DataFrame({"truck_id": ["CAM-001"], "tanque_l": [600]})
    ev = parking_drop_events(gps, trucks).iloc[0]
    assert ev["hasta"] == gps["ts"][4] and ev["desde"] == gps["ts"][3]
    assert abs(ev["caida_l"] - 120) < 6
