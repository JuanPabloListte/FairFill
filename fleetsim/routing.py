"""Obtención de rutas: ORS (perfil para camiones) u OSRM demo (perfil auto), con caché en disco."""
import hashlib
import json
import os
import time
from pathlib import Path

import requests

from .geo import Route

ORS_URL = "https://api.openrouteservice.org/v2/directions/driving-hgv/geojson"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{o_lon},{o_lat};{d_lon},{d_lat}"
USER_AGENT = "fleet-sim/0.1 (proyecto educativo)"


class RouteProvider:
    def __init__(self, routing_cfg: dict, cache_dir: Path):
        self.cfg = routing_cfg
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ors_key = os.environ.get("ORS_API_KEY")
        provider = routing_cfg["provider"]
        if provider == "auto":
            provider = "ors" if self.ors_key else "osrm"
        if provider == "ors" and not self.ors_key:
            raise RuntimeError("provider = 'ors' requiere la variable de entorno ORS_API_KEY")
        self.provider = provider
        self._last_call = 0.0

    def route(self, origin: dict, dest: dict, truck_type: dict, avoid_tolls: bool = False) -> Route:
        if avoid_tolls and self.provider != "ors":
            raise RuntimeError("evitar peajes requiere ORS (el servidor demo de OSRM no lo permite): definí ORS_API_KEY")
        # Con OSRM la ruta no depende del camión; con ORS sí (restricciones de altura, peso, etc.).
        profile = (
            {k: truck_type[k] for k in ("height_m", "width_m", "length_m", "weight_t", "axleload_t")}
            if self.provider == "ors" else {}
        )
        key = [self.provider, origin["lat"], origin["lon"], dest["lat"], dest["lon"], profile]
        if avoid_tolls:  # solo se agrega si hace falta, para no invalidar la caché existente
            key.append("sin_peajes")
        key_src = json.dumps(key, sort_keys=True)
        path = self.cache_dir / f"{hashlib.sha1(key_src.encode()).hexdigest()[:16]}.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            raw = self._fetch_ors(origin, dest, profile, avoid_tolls) if self.provider == "ors" else self._fetch_osrm(origin, dest)
            raw["origin"], raw["dest"] = origin["name"], dest["name"]
            path.write_text(json.dumps(raw), encoding="utf-8")

        factor = self.cfg[f"{raw['source']}_duration_factor"]
        return Route.from_coords(raw["coords_latlon"], raw["duration_s"] / 3600 * factor, raw["source"])

    def _throttle(self, min_interval_s: float):
        wait = self._last_call + min_interval_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _fetch_osrm(self, o, d) -> dict:
        self._throttle(1.1)  # el servidor demo pide uso moderado
        url = OSRM_URL.format(o_lon=o["lon"], o_lat=o["lat"], d_lon=d["lon"], d_lat=d["lat"])
        resp = requests.get(
            url, params={"overview": "full", "geometries": "geojson"},
            headers={"User-Agent": USER_AGENT}, timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok":
            raise RuntimeError(f"OSRM sin ruta {o['name']} -> {d['name']}: {data.get('code')}")
        r = data["routes"][0]
        return {
            "source": "osrm",
            "coords_latlon": [[lat, lon] for lon, lat in r["geometry"]["coordinates"]],
            "distance_m": r["distance"],
            "duration_s": r["duration"],
        }

    def _fetch_ors(self, o, d, profile, avoid_tolls=False) -> dict:
        self._throttle(1.6)  # plan gratuito: 40 consultas por minuto
        body = {
            "coordinates": [[o["lon"], o["lat"]], [d["lon"], d["lat"]]],
            "instructions": False,
            "extra_info": ["tollways"],
            "options": {
                "vehicle_type": "hgv",
                **({"avoid_features": ["tollways"]} if avoid_tolls else {}),
                "profile_params": {"restrictions": {
                    "height": profile["height_m"], "width": profile["width_m"],
                    "length": profile["length_m"], "weight": profile["weight_t"],
                    "axleload": profile["axleload_t"],
                }},
            },
        }
        resp = requests.post(
            ORS_URL, json=body, timeout=60,
            headers={"Authorization": self.ors_key, "User-Agent": USER_AGENT},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"ORS {resp.status_code} {o['name']} -> {d['name']}: {resp.text[:300]}")
        feat = resp.json()["features"][0]
        summary = feat["properties"]["summary"]
        return {
            "source": "ors",
            "coords_latlon": [[lat, lon] for lon, lat, *_ in feat["geometry"]["coordinates"]],
            "distance_m": summary["distance"],
            "duration_s": summary["duration"],
            "tollways": feat["properties"].get("extras", {}).get("tollways"),
        }
