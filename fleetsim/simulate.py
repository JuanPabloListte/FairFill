"""Motor de simulación: camiones que viajan por rutas reales, cargan en estaciones reales y cruzan peajes reales.

Genera lo que una empresa de transporte tendría en sus sistemas (tarjeta de combustible,
TelePASE, GPS) y, por separado, la "verdad": parámetros ocultos, anomalías inyectadas y
eventos legítimos que se parecen a anomalías.
"""
import string
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .corridor import stations_on_route, tolls_on_route
from .geo import Route, haversine_km
from .market import PriceModel, TollTariff


@dataclass
class Driver:
    driver_id: str
    efficiency: float  # > 1 consume más que la ficha técnica


@dataclass
class Sensor:
    """Sensor de nivel: lectura = offset + deriva + ganancia * nivel real + ruido.

    Puede quedar trabado (repite la última lectura) durante algunas horas.
    """
    gain: float
    offset_pct: float
    noise_pct: float
    drift_pct: float = 0.0
    last_t: datetime | None = None
    last_reading: float | None = None
    stuck_until: datetime | None = None


@dataclass
class Truck:
    truck_id: str
    patente: str
    type_name: str
    spec: dict
    efficiency: float
    driver: Driver
    tag_id: str
    card_id: str
    sensor: Sensor
    reefer: str | None = None       # None, "tanque_propio" o "tanque_principal"
    reefer_level_l: float = 0.0

    def liters_per_km(self, load_ratio: float) -> float:
        s = self.spec
        l100 = s["l_100km_empty"] + (s["l_100km_full"] - s["l_100km_empty"]) * load_ratio
        return l100 / 100 * self.efficiency


@dataclass
class State:
    t: datetime
    level_l: float
    odometer_km: float
    lat: float
    lon: float
    driving_today_h: float = 0.0


@dataclass
class RouteContext:
    route: Route
    stations: pd.DataFrame   # estaciones en el corredor, ordenadas por km
    tolls: list = field(default_factory=list)


class Simulator:
    def __init__(self, cfg: dict, stations: pd.DataFrame, booths: pd.DataFrame, router, rng: np.random.Generator):
        self.cfg = cfg
        self.ops = cfg["operations"]
        self.fuel = cfg["fuel"]
        self.an = cfg["anomalies"]
        self.real = cfg["realism"]
        self.stations = stations
        self.booths = booths
        self.router = router
        self.rng = rng
        self.start = datetime.fromisoformat(cfg["start_date"])
        self.end = self.start + timedelta(days=cfg["days"])
        self.prices = PriceModel(stations, self.fuel, self.start, cfg["days"], rng)
        self.tariff = TollTariff(cfg["tolls"])
        self.depot = {"name": cfg["depot"]["name"], "lat": cfg["depot"]["lat"], "lon": cfg["depot"]["lon"]}
        self.dests = cfg["destinations"]
        # Estaciones cuyo sistema informa las transacciones con horas de demora.
        n_delayed = int(round(len(stations) * self.real["delayed_posting_station_share"]))
        self.delayed_stations = set(rng.choice(stations["station_id"].to_numpy(), n_delayed, replace=False)) if n_delayed else set()
        self._route_cache: dict = {}
        self._counters: dict = {}
        self.out = {k: [] for k in ("trips", "fuel_tx", "tolls", "gps", "anomalies", "legit")}
        self.stats = {"ran_dry": 0}

    # ---------- utilidades ----------
    def _next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}{self._counters[prefix]:06d}"

    def _uniform(self, pair):
        return float(self.rng.uniform(*pair))

    # ---------- flota ----------
    def build_fleet(self) -> list[Truck]:
        fc, real = self.cfg["fleet"], self.real
        types = self.cfg["truck_types"]
        names = list(types)
        shares = np.array([types[n]["share"] for n in names], dtype=float)
        counts = np.floor(shares / shares.sum() * fc["size"]).astype(int)
        counts[np.argmax(shares)] += fc["size"] - counts.sum()

        self.drivers = [
            Driver(f"CH-{i + 1:03d}", float(self.rng.normal(1.0, fc["driver_efficiency_sd"])))
            for i in range(fc["size"] + fc["extra_drivers"])
        ]
        trucks = []
        letters = string.ascii_uppercase
        for name, n in zip(names, counts):
            for _ in range(n):
                i = len(trucks)
                # Prefijo ZZ: formato Mercosur que todavía no se emite, para no coincidir con patentes reales.
                patente = f"ZZ{self.rng.integers(100, 1000)}{letters[self.rng.integers(26)]}{letters[self.rng.integers(26)]}"
                sensor = Sensor(
                    gain=float(self.rng.normal(1.0, real["sensor_gain_sd"])),
                    offset_pct=float(self.rng.normal(0.0, real["sensor_offset_sd_pct"])),
                    noise_pct=self._uniform(real["sensor_noise_pct"]),
                )
                reefer = None
                if types[name]["axles"] >= 5 and self.rng.random() < real["reefer_share_of_semis"]:
                    reefer = "tanque_principal" if self.rng.random() < real["reefer_main_tank_prob"] else "tanque_propio"
                trucks.append(Truck(
                    truck_id=f"CAM-{i + 1:03d}", patente=patente, type_name=name, spec=types[name],
                    efficiency=float(self.rng.normal(1.0, fc["truck_efficiency_sd"])),
                    driver=self.drivers[i], tag_id=f"TP-{i + 1:05d}", card_id=f"TF-{i + 1:05d}",
                    sensor=sensor, reefer=reefer, reefer_level_l=real["reefer_tank_l"] if reefer else 0.0,
                ))
        self.trucks = trucks
        return trucks

    # ---------- rutas ----------
    def route_context(self, origin: dict, dest: dict, truck: Truck) -> RouteContext:
        key = (origin["name"], dest["name"], truck.type_name if self.router.provider == "ors" else "")
        if key in self._route_cache:
            return self._route_cache[key]
        route = self.router.route(origin, dest, truck.spec)
        tc = self.cfg["tolls"]
        ctx = RouteContext(
            route,
            stations_on_route(route, self.stations, self.fuel["corridor_km"]),
            tolls_on_route(route, self.booths, tc["match_radius_km"], tc["merge_km"]),
        )
        self._route_cache[key] = ctx
        return ctx

    # ---------- registros ----------
    def _sensor_reading(self, truck: Truck, st: State) -> float:
        s, real = truck.sensor, self.real
        if s.last_t is not None:
            days = max((st.t - s.last_t).total_seconds() / 86400, 0.0)
            s.drift_pct += float(self.rng.normal(0, real["sensor_drift_pct_per_sqrt_day"] * np.sqrt(days)))
            if s.stuck_until is None and self.rng.random() < real["sensor_stuck_prob_per_day"] * days:
                s.stuck_until = st.t + timedelta(hours=self._uniform(real["sensor_stuck_hours"]))
                self._legit("sensor_trabado", truck, st.t, hasta=s.stuck_until)
        s.last_t = st.t
        if s.stuck_until is not None:
            if st.t < s.stuck_until and s.last_reading is not None:
                return s.last_reading
            s.stuck_until = None
        true_pct = st.level_l / truck.spec["tank_l"] * 100
        reading = s.offset_pct + s.drift_pct + s.gain * true_pct + self.rng.normal(0, s.noise_pct)
        s.last_reading = round(float(np.clip(reading, 0, 100)), 1)
        return s.last_reading

    def _gps(self, truck: Truck, st: State, speed_kmh: float, ignition: bool):
        # El sensor se actualiza aunque el ping se pierda, para que la deriva siga su curso.
        level_pct = self._sensor_reading(truck, st)
        if self.rng.random() < self.ops["gps_drop_rate"]:
            return
        noise = self.rng.normal(0, 0.00005, 2)  # ~5 m
        self.out["gps"].append({
            "truck_id": truck.truck_id, "ts": st.t, "lat": round(st.lat + noise[0], 6),
            "lon": round(st.lon + noise[1], 6),
            "speed_kmh": round(max(0.0, speed_kmh + (self.rng.normal(0, 4) if speed_kmh else 0)), 1),
            "ignition": ignition, "odometer_km": round(st.odometer_km, 1),
            "fuel_level_pct": level_pct,
        })

    def _reported_odometer(self, true_km: float) -> int:
        km = int(round(true_km + self.rng.normal(0, 3)))
        if self.rng.random() < self.ops["odometer_typo_prob"]:
            s = str(km)
            i = int(self.rng.integers(len(s) - 1))
            s = s[:i] + s[i + 1] + s[i] + s[i + 2:] if self.rng.random() < 0.5 else s[:i] + s[i + 1:]
            km = int(s)
        return km

    def _fuel_tx(self, card_truck: Truck, driver, station, when, liters, odometer_km, trip_id, discount=True):
        """Registra una transacción de la tarjeta de `card_truck` (que puede no ser el camión que cargó)."""
        unit = self.prices.price(station, when)
        if discount and station.bandera == self.fuel["preferred_brand"]:
            unit *= 1 - self.fuel["preferred_discount"]
        posted = when + timedelta(minutes=float(self.rng.normal(0, self.real["tx_clock_jitter_min"])))
        tx_id = self._next_id("TX")
        if station.station_id in self.delayed_stations:
            delay_h = self._uniform(self.real["delayed_posting_hours"])
            posted += timedelta(hours=delay_h)
            self._legit("posteo_demorado", card_truck, when, tx_id=tx_id,
                        detalle=f"la estación informó la transacción {delay_h:.1f} h después")
        self.out["fuel_tx"].append({
            "tx_id": tx_id, "ts": posted, "card_id": card_truck.card_id, "truck_id": card_truck.truck_id,
            "patente": card_truck.patente, "driver_id": driver.driver_id, "trip_id": trip_id,
            "station_idempresa": station.idempresa, "estacion": station.empresa, "bandera": station.bandera,
            "direccion": station.direccion, "localidad": station.localidad, "provincia": station.provincia,
            "producto": self.fuel["product"], "litros": round(liters, 2), "precio_unitario": round(unit, 2),
            "importe": round(unit * liters, 2), "odometro_informado": self._reported_odometer(odometer_km),
        })
        return tx_id

    def _anomaly(self, kind, truck, when, **extra):
        self.out["anomalies"].append({
            "anomaly_id": self._next_id("AN"), "tipo": kind, "truck_id": truck.truck_id, "ts": when, **extra,
        })

    def _legit(self, kind, truck, when, **extra):
        """Evento que no es fraude pero puede parecerlo: sirve para medir falsos positivos por causa."""
        self.out["legit"].append({
            "event_id": self._next_id("LG"), "tipo": kind, "truck_id": truck.truck_id, "ts": when, **extra,
        })

    # ---------- eventos ----------
    def _plan_theft(self, truck: Truck, start: datetime, until: datetime):
        """Devuelve [(momento, litros)]: un robo de golpe o repartido en varias lecturas."""
        if self.rng.random() >= self.an["siphon_prob"]:
            return []
        tank = truck.spec["tank_l"]
        total = self._uniform((self.an["siphon_min_l"], max(self.an["siphon_min_l"] + 1, self.an["siphon_max_tank_frac"] * tank)))
        at = start + (until - start) * self._uniform((0.1, 0.6))
        if self.rng.random() < self.an["siphon_gradual_prob"]:
            n = int(self.rng.integers(3, 7))
            step = timedelta(minutes=self.ops["parked_ping_min"])
            return [(at + k * step, total / n) for k in range(n)]
        return [(at, total)]

    def park(self, truck: Truck, st: State, until: datetime, motivo: str, reefer_on: bool = False):
        """Camión detenido: pings con motor apagado, posible robo y, si lleva frío, consumo del equipo."""
        if until <= st.t:
            return
        parked_since = st.t
        thefts = self._plan_theft(truck, st.t, until)
        stolen, theft_start, theft_end = 0.0, None, None
        reefer_main = reefer_on and truck.reefer == "tanque_principal"
        if reefer_main:
            self._legit("frio_con_tanque_principal", truck, st.t, hasta=until,
                        detalle=f"equipo de frío consume {self.real['reefer_l_per_h']} L/h del tanque principal")
        step = timedelta(minutes=self.ops["parked_ping_min"])
        t = st.t
        while t + step <= until:
            t += step
            if reefer_on:
                used = self.real["reefer_l_per_h"] * step.total_seconds() / 3600
                if reefer_main:
                    st.level_l = max(st.level_l - used, 0.0)
                elif truck.reefer == "tanque_propio":
                    truck.reefer_level_l = max(truck.reefer_level_l - used, 0.0)
            while thefts and thefts[0][0] <= t:
                when, liters = thefts.pop(0)
                liters = min(liters, st.level_l - 10)
                if liters > 0:
                    st.level_l -= liters
                    stolen += liters
                    theft_start = theft_start or when
                    theft_end = when
            st.t = t
            self._gps(truck, st, 0.0, False)
        if stolen > 0:
            self._anomaly("sifonado", truck, theft_start, ts_fin=theft_end, litros=round(stolen, 1),
                          lat=st.lat, lon=st.lon,
                          detalle=f"caída sin carga durante {motivo}" + (" (gradual)" if theft_end > theft_start else ""))
        if until - parked_since >= timedelta(hours=self.ops["rest_hours"]):
            st.driving_today_h = 0.0
        st.t = until

    def choose_station(self, truck, st, ctx: RouteContext, km: float, rate: float):
        stations = ctx.stations
        reach = st.level_l / rate * 0.85
        ahead = stations[(stations["km"] > km) & (stations["km"] <= km + reach)]
        if ahead.empty:
            ahead = stations[stations["km"] > km].head(1)
            if ahead.empty:
                return None
        pref = ahead[ahead["bandera"] == self.fuel["preferred_brand"]]
        if not pref.empty and self.rng.random() < self.fuel["preferred_brand_prob"]:
            return pref.iloc[0]
        return ahead.iloc[0]

    def refuel(self, truck, st: State, station, rate, trip_id, driver):
        tank = truck.spec["tank_l"]
        # Desvío ida y vuelta desde la ruta hasta la estación.
        detour = 2 * float(station.offset_km)
        st.level_l -= detour * rate
        st.odometer_km += detour
        st.t += timedelta(hours=detour / 40)
        st.lat, st.lon = float(station.lat), float(station.lon)
        self._gps(truck, st, 0.0, False)

        level_before = st.level_l
        target = tank if self.rng.random() < self.fuel["full_fill_prob"] else max(level_before, self._uniform((0.5, 0.8)) * tank)
        liters = target - level_before
        stop = timedelta(minutes=self._uniform(self.ops["fuel_stop_minutes"]))
        if liters < 10:
            st.t += stop
            return
        tx_time = st.t + stop * self._uniform((0.3, 0.7))

        # A veces el chofer paga con la tarjeta de otro camión de la flota (la suya no anda, etc.).
        card_truck = truck
        if self.rng.random() < self.real["shared_card_prob"]:
            card_truck = self.trucks[int(self.rng.choice([i for i, t in enumerate(self.trucks) if t is not truck]))]

        reported = liters
        overfill = self.rng.random() < self.an["overfill_prob"]
        if overfill:
            reported = (tank - level_before) + self._uniform(self.an["overfill_extra"]) * tank
        tx_id = self._fuel_tx(card_truck, driver, station, tx_time, reported, st.odometer_km, trip_id)
        if card_truck is not truck:
            self._legit("tarjeta_compartida", truck, tx_time, tx_id=tx_id,
                        detalle=f"cargó {truck.truck_id} con la tarjeta de {card_truck.truck_id}")
        if overfill:
            self._anomaly("sobrecarga_tanque", truck, tx_time, tx_id=tx_id, litros=round(reported - liters, 1),
                          lat=st.lat, lon=st.lon, detalle="litros facturados mayores a la capacidad libre del tanque")
        st.level_l = target

        # Equipo de frío con tanque propio: carga aparte que el sensor del camión no ve.
        if truck.reefer == "tanque_propio" and truck.reefer_level_l < 0.6 * self.real["reefer_tank_l"]:
            r_liters = self.real["reefer_tank_l"] - truck.reefer_level_l
            r_time = tx_time + timedelta(minutes=self._uniform((2, 8)))
            r_id = self._fuel_tx(card_truck, driver, station, r_time, r_liters, st.odometer_km, trip_id)
            truck.reefer_level_l = self.real["reefer_tank_l"]
            self._legit("carga_equipo_frio", truck, r_time, tx_id=r_id,
                        detalle=f"{r_liters:.0f} L al tanque del equipo de frío, no medido por el sensor")

        if self.rng.random() < self.an["duplicate_prob"]:
            dup_time = tx_time + timedelta(minutes=self._uniform((10, 40)))
            dup_l = self._uniform((0.1, 0.3)) * tank
            dup_id = self._fuel_tx(card_truck, driver, station, dup_time, dup_l, st.odometer_km, trip_id)
            self._anomaly("carga_duplicada", truck, dup_time, tx_id=dup_id, litros=round(dup_l, 1),
                          lat=st.lat, lon=st.lon, detalle=f"segunda transacción tras {tx_id} con tanque lleno")

        st.t += stop
        self._gps(truck, st, 0.0, False)

    def drive_leg(self, truck, st: State, ctx: RouteContext, load_ratio, trip_id, driver, reefer_on: bool):
        route, ops = ctx.route, self.ops
        speed = route.length_km / route.duration_h
        rate = truck.liters_per_km(load_ratio) * driver.efficiency * float(self.rng.normal(1, self.cfg["fleet"]["trip_noise_sd"]))
        reefer_lph = self.real["reefer_l_per_h"] if reefer_on else 0.0
        if truck.reefer == "tanque_principal":
            rate += reefer_lph / speed
        tank = truck.spec["tank_l"]
        dt_h = ops["gps_interval_min"] / 60
        km, length = 0.0, route.length_km
        target, toll_i = None, 0

        while km < length - 1e-6:
            if target is None and st.level_l < self.fuel["refuel_threshold"] * tank:
                target = self.choose_station(truck, st, ctx, km, rate)
            next_km, reason = min(km + speed * dt_h, length), None
            if target is not None and target["km"] <= next_km:
                next_km, reason = max(float(target["km"]), km), "fuel"
            cap_left = max(ops["daily_driving_hours"] - st.driving_today_h, 0.0)
            if (next_km - km) / speed > cap_left:
                next_km, reason = km + cap_left * speed, "rest"

            while toll_i < len(ctx.tolls) and ctx.tolls[toll_i]["km"] <= next_km:
                tc = ctx.tolls[toll_i]
                when = st.t + timedelta(hours=max(tc["km"] - km, 0) / speed)
                category = truck.spec["toll_category"]
                amount, peak = self.tariff.amount(category, when)
                self.out["tolls"].append({
                    "crossing_id": self._next_id("PJ"), "ts": when, "tag_id": truck.tag_id,
                    "truck_id": truck.truck_id, "patente": truck.patente, "osm_id": tc["osm_id"],
                    "cabina": tc["nombre"], "operador": tc["operador"], "lat": tc["lat"], "lon": tc["lon"],
                    "categoria": category, "horario": "pico" if peak else "normal", "importe": amount,
                })
                toll_i += 1

            seg = next_km - km
            st.level_l -= seg * rate
            st.odometer_km += seg
            st.t += timedelta(hours=seg / speed)
            st.driving_today_h += seg / speed
            if truck.reefer == "tanque_propio":
                truck.reefer_level_l = max(truck.reefer_level_l - reefer_lph * seg / speed, 0.0)
            km = next_km
            st.lat, st.lon = route.position_at(km)
            if st.level_l < 0:
                self.stats["ran_dry"] += 1
                st.level_l = 0.0
            if seg > 0:
                self._gps(truck, st, speed, True)

            if reason == "fuel":
                self.refuel(truck, st, target, rate, trip_id, driver)
                st.lat, st.lon = route.position_at(km)
                target = None
            elif reason == "rest":
                self.park(truck, st, st.t + timedelta(hours=ops["rest_hours"]), "descanso", reefer_on)
                st.driving_today_h = 0.0

    # ---------- bucle principal ----------
    def run_truck(self, truck: Truck):
        ops = self.ops
        st = State(
            t=self.start, level_l=self._uniform((0.5, 1.0)) * truck.spec["tank_l"],
            odometer_km=self._uniform(ops["odometer_start_km"]),
            lat=self.depot["lat"], lon=self.depot["lon"],
        )
        weights = np.array([d["weight"] for d in self.dests], dtype=float)
        weights /= weights.sum()
        next_depart = self.start + timedelta(hours=self._uniform(ops["depart_hour"]))

        while next_depart < self.end:
            self.park(truck, st, next_depart, "base")
            st.driving_today_h = 0.0
            dest = self.dests[self.rng.choice(len(self.dests), p=weights)]
            substitute = self.rng.random() < self.cfg["fleet"]["substitute_driver_prob"]
            driver = self.drivers[self.rng.integers(len(self.drivers))] if substitute else truck.driver

            # El frío solo funciona en la ida, cuando el camión va cargado.
            legs = ((self.depot, dest, ops["outbound_load"], True), (dest, self.depot, ops["return_load"], False))
            for origin, target, load_rng, reefer_on in legs:
                ctx = self.route_context(origin, target, truck)
                load = self._uniform(load_rng)
                trip_id = self._next_id("VJ")
                depart, odo0 = st.t, st.odometer_km
                self.drive_leg(truck, st, ctx, load, trip_id, driver, reefer_on and truck.reefer is not None)
                self.out["trips"].append({
                    "trip_id": trip_id, "truck_id": truck.truck_id, "driver_id": driver.driver_id,
                    "origen": origin["name"], "destino": target["name"], "salida": depart, "llegada": st.t,
                    "km_ruta": round(ctx.route.length_km, 1), "km_odometro": round(st.odometer_km - odo0, 1),
                    "carga_kg": round(load * truck.spec["payload_kg"]), "fuente_ruta": ctx.route.source,
                })
                if target is dest:
                    self.park(truck, st, st.t + timedelta(hours=self._uniform(ops["unload_hours"])), "descarga")

            rest_days = int(self.rng.integers(ops["depot_rest_days"][0], ops["depot_rest_days"][1] + 1))
            day = datetime.combine(st.t.date(), datetime.min.time()) + timedelta(days=rest_days)
            next_depart = day + timedelta(hours=self._uniform(ops["depart_hour"]))
            while next_depart < st.t + timedelta(hours=ops["rest_hours"]):
                next_depart += timedelta(days=1)

        self.park(truck, st, max(self.end, st.t), "base")

    def inject_foreign_card_use(self, trucks: list[Truck]):
        """Tarjeta usada en una estación lejana a donde el GPS ubica al camión (clonación o préstamo)."""
        gps = pd.DataFrame(self.out["gps"])
        for truck in trucks:
            g = gps[gps["truck_id"] == truck.truck_id].sort_values("ts")
            if g.empty:
                continue
            for d in range(self.cfg["days"]):
                if self.rng.random() >= self.an["foreign_card_prob"]:
                    continue
                when = self.start + timedelta(days=d, hours=self._uniform((6, 22)))
                pos = g.iloc[int(np.argmin(np.abs((g["ts"] - when).dt.total_seconds().to_numpy())))]
                dist = haversine_km(pos["lat"], pos["lon"], self.stations["lat"].to_numpy(), self.stations["lon"].to_numpy())
                cand = self.stations[(dist > self.an["foreign_min_km"]) & (dist < 400)]
                if cand.empty:
                    continue
                station = cand.iloc[int(self.rng.integers(len(cand)))]
                liters = self._uniform((80, 250))
                tx_id = self._fuel_tx(truck, truck.driver, station, when, liters, pos["odometer_km"], None)
                self._anomaly("tarjeta_fuera_de_ruta", truck, when, tx_id=tx_id, litros=round(liters, 1),
                              lat=float(station.lat), lon=float(station.lon),
                              detalle=f"estación a {dist[station.name]:.0f} km de la posición GPS")

    def run(self):
        trucks = self.build_fleet()
        for truck in trucks:
            print(f"  simulando {truck.truck_id} ({truck.type_name}{', frío ' + truck.reefer if truck.reefer else ''})")
            self.run_truck(truck)
        self.inject_foreign_card_use(trucks)
        return trucks
