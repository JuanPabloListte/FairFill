import itertools

import numpy as np
import pytest

from planner.optimize import Problem, Stop, simulate_policy, solve


def _problem(**kw):
    base = dict(length_km=1000, rate_l_km=0.4, tank_l=300, start_l=100, reserve_l=20,
                end_reserve_l=20, end_value=0.0)
    return Problem(**{**base, **kw})


def test_buys_just_enough_before_the_cheap_station():
    stops = [Stop(100, 0, 100, 0), Stop(500, 0, 80, 0), Stop(800, 0, 120, 0)]
    res = solve(_problem(), stops)
    # En A carga lo justo para llegar a B con reserva (120 L); en B, lo justo para destino (200 L).
    assert res["cost"] == pytest.approx(120 * 100 + 200 * 80)
    assert [(j, round(l)) for j, l, _ in res["plan"]] == [(0, 120), (1, 200)]


def test_stop_cost_avoids_an_extra_stop():
    # Parar en B ahorra 20 $/L pero cuesta 10.000 $: conviene cargar todo en A.
    stops = [Stop(100, 0, 100, 0), Stop(500, 0, 80, 10_000)]
    res = solve(_problem(start_l=60, length_km=700, tank_l=400), stops)
    assert len(res["plan"]) == 1 and res["plan"][0][0] == 0


def test_infeasible_route_is_reported():
    res = solve(_problem(start_l=50), [Stop(900, 0, 100, 0)])
    assert res["cost"] == np.inf


def test_leftover_fuel_has_value():
    # Con valor de reposición alto, conviene llenar en la estación barata aunque sobre combustible.
    stops = [Stop(100, 0, 80, 0)]
    cheap_left = solve(_problem(length_km=600, end_value=150), stops)
    no_value = solve(_problem(length_km=600, end_value=0), stops)
    assert cheap_left["plan"][0][1] > no_value["plan"][0][1]


@pytest.mark.parametrize("seed", range(20))
def test_matches_brute_force_on_small_instances(seed):
    rng = np.random.default_rng(seed)
    kms = np.sort(rng.choice(np.arange(50, 600, 50), 3, replace=False)).astype(float)
    stops = [Stop(k, 0.0, float(rng.integers(70, 130)), float(rng.choice([0, 500]))) for k in kms]
    p = _problem(length_km=600, rate_l_km=0.5, tank_l=200, start_l=60, reserve_l=10, end_reserve_l=10)
    best = np.inf
    grid = range(0, 201, 5)
    for buys in itertools.product(grid, repeat=3):
        fuel, pos, cost, ok = p.start_l, 0.0, 0.0, True
        for s, b in zip(stops, buys):
            fuel -= p.rate_l_km * (s.km - pos)
            pos = s.km
            if b and fuel < p.reserve_l or fuel + b > p.tank_l or fuel < 0:
                ok = False
                break
            if b:
                cost += b * s.price + s.stop_cost
                fuel += b
        fuel -= p.rate_l_km * (p.length_km - pos)
        if ok and fuel >= p.end_reserve_l:
            best = min(best, cost)
    res = solve(p, stops)
    assert res["cost"] == pytest.approx(best, abs=1e-6)


def test_policy_refuels_to_full_below_threshold():
    stops = [Stop(100, 0, 100, 0), Stop(500, 0, 80, 0)]
    res = simulate_policy(_problem(), stops, threshold_frac=0.35)
    assert res["cost"] < np.inf and all(round(arr + l) == 300 for _, l, arr in res["plan"])


def test_minimum_purchase_avoids_tiny_stops():
    # A (km 100, cara) y B (km 200, barata). Sale con 60 L y gasta 0,4 L/km: llega a A con 20 L.
    # Sin mínimo carga en A justo 30 L para llegar a B con reserva; con mínimo de 50 L carga 50.
    stops = [Stop(100, 0, 100, 0), Stop(200, 0, 50, 0)]
    p = dict(length_km=400, rate_l_km=0.4, tank_l=300, start_l=60, reserve_l=10, end_reserve_l=10)
    free = solve(_problem(**p), stops)
    assert [(j, round(l)) for j, l, _ in free["plan"]][0] == (0, 30)
    capped = solve(_problem(**p, min_buy_l=50), stops)
    assert [(j, round(l)) for j, l, _ in capped["plan"]][0] == (0, 50)
    assert all(l >= 50 - 1e-6 for _, l, _ in capped["plan"])
