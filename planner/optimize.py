"""Dónde y cuánto cargar sobre una ruta fija: programación dinámica.

Un resultado clásico del problema de la estación de servicio: en una solución óptima, en cada
parada se llena el tanque o se carga justo lo necesario para llegar a otra parada con la
reserva mínima. Eso deja pocos estados por estación ("combustible al llegar") y hace exacto
el problema con programación dinámica, aun con un costo fijo por parada.

Convenciones: litros, km, pesos. El desvío hasta la estación (ida y vuelta desde la ruta)
se consume solo si se para ahí.
"""
from dataclasses import dataclass

import numpy as np


@dataclass
class Stop:
    km: float
    offset_km: float
    price: float          # precio para decidir (puede incluir prima por precio estimado)
    stop_cost: float      # costo fijo de parar: tiempo y desvío, en pesos
    ref: object = None    # fila de la estación, para mostrar


@dataclass
class Problem:
    length_km: float
    rate_l_km: float      # consumo, con margen de seguridad
    tank_l: float
    start_l: float
    reserve_l: float      # mínimo al llegar a cualquier estación
    end_reserve_l: float  # mínimo al llegar a destino
    end_value: float      # valor por litro que sobra al llegar (se usa en el próximo viaje)
    min_buy_l: float = 0.0  # carga mínima por parada: nadie para a cargar 10 litros


def _key(x: float) -> float:
    return round(x, 1)


def solve(p: Problem, stops: list[Stop]) -> dict:
    """Devuelve {"cost", "plan": [(índice de parada, litros)], "end_l"} o {"cost": inf} si no hay plan posible."""
    n = len(stops)
    kms = np.array([s.km for s in stops])
    # best[j] = {combustible al llegar: (costo, (j_prev, llegada_prev, litros_cargados))}
    best: list[dict] = [dict() for _ in range(n)]
    finish = {"cost": np.inf}

    def relax(j, arrival, cost, back):
        k = _key(arrival)
        if cost < best[j].get(k, (np.inf,))[0]:
            best[j][k] = (cost, back)

    def depart(i_pos, fuel, cost, back_from):
        """Desde la posición km `i_pos` con `fuel` litros: ir a cada parada posterior o a destino."""
        # A destino directo.
        left = fuel - p.rate_l_km * (p.length_km - i_pos)
        if left >= p.end_reserve_l - 1e-6:
            total = cost - (left - p.end_reserve_l) * p.end_value
            if total < finish["cost"]:
                finish.update(cost=total, back=back_from, end_l=left)
        for k in range(np.searchsorted(kms, i_pos, side="right"), n):
            arrival = fuel - p.rate_l_km * (kms[k] - i_pos) - p.rate_l_km * stops[k].offset_km
            if arrival < p.reserve_l - 1e-6:
                break  # las siguientes quedan más lejos
            relax(k, arrival, cost, back_from)

    depart(0.0, p.start_l, 0.0, None)
    for j in range(n):
        s = stops[j]
        back_offset = p.rate_l_km * s.offset_km  # volver a la ruta
        for arr_key, (cost, _) in list(best[j].items()):
            arrival = arr_key
            targets = {p.tank_l}
            # Justo lo necesario para llegar con reserva a cada parada posterior o a destino.
            for k in range(j + 1, n):
                targets.add(back_offset + p.rate_l_km * (kms[k] - s.km + stops[k].offset_km) + p.reserve_l)
            targets.add(back_offset + p.rate_l_km * (p.length_km - s.km) + p.end_reserve_l)
            if p.min_buy_l:
                targets.add(arrival + p.min_buy_l)  # si lo justo es menos que el mínimo, cargar el mínimo
            for target in targets:
                if target > p.tank_l + 1e-6 or target < arrival + max(p.min_buy_l, 1e-6) - 1e-6:
                    continue
                liters = target - arrival
                new_cost = cost + liters * s.price + s.stop_cost
                depart(s.km, target - back_offset, new_cost, (j, arr_key, liters))

    if not np.isfinite(finish["cost"]):
        return {"cost": np.inf}
    plan, back = [], finish["back"]
    while back is not None:
        j, arr_key, liters = back
        plan.append((j, liters, arr_key))
        back = best[j][arr_key][1]
    return {"cost": finish["cost"], "plan": plan[::-1], "end_l": finish["end_l"]}


def simulate_policy(p: Problem, stops: list[Stop], threshold_frac: float, prefer=None) -> dict:
    """Lo que suele hacer un chofer: cuando el tanque baja del umbral, llena en la primera estación
    que encuentra (la primera de la bandera preferida, si hay alguna a su alcance). Sirve de comparación."""
    fuel, pos, plan, cost = p.start_l, 0.0, [], 0.0
    while True:
        trigger = pos + (fuel - threshold_frac * p.tank_l) / p.rate_l_km
        left = fuel - p.rate_l_km * (p.length_km - pos)
        if trigger >= p.length_km and left >= p.end_reserve_l:
            return {"cost": cost - (left - p.end_reserve_l) * p.end_value, "plan": plan, "end_l": left}
        reach = pos + (fuel - p.reserve_l) / p.rate_l_km
        reachable = [j for j, st in enumerate(stops) if pos < st.km and st.km + st.offset_km <= reach]
        cands = [j for j in reachable if stops[j].km >= trigger]
        if not cands:
            if left >= p.end_reserve_l:  # bajó del umbral pero no hay más estaciones y alcanza para llegar
                return {"cost": cost - (left - p.end_reserve_l) * p.end_value, "plan": plan, "end_l": left}
            cands = reachable  # no alcanza: carga en la próxima que pueda
        if not cands:
            return {"cost": np.inf}
        pick = next((j for j in cands if prefer and prefer(stops[j])), cands[0])
        st = stops[pick]
        arrival = fuel - p.rate_l_km * (st.km - pos + st.offset_km)
        liters = p.tank_l - arrival
        cost += liters * st.price + st.stop_cost
        plan.append((pick, liters, arrival))
        fuel, pos = p.tank_l - p.rate_l_km * st.offset_km, st.km
