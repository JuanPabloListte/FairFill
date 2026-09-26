"""Qué estaciones y qué cabinas de peaje quedan sobre una ruta. Lo usan el simulador y el planificador."""
import numpy as np
import pandas as pd

from .geo import Route, project_points


def stations_on_route(route: Route, stations: pd.DataFrame, corridor_km: float) -> pd.DataFrame:
    """Estaciones a menos de `corridor_km` de la ruta, con el km donde quedan y la distancia a la ruta."""
    dist, at_km = project_points(route, stations["lat"], stations["lon"], corridor_km)
    on = np.isfinite(dist)
    st = stations[on].copy()
    st["km"] = at_km[on]
    st["offset_km"] = dist[on]
    return st.sort_values("km").reset_index(drop=True)


def tolls_on_route(route: Route, booths: pd.DataFrame, match_radius_km: float, merge_km: float) -> list[dict]:
    """Cabinas sobre la ruta. OSM suele tener un nodo por sentido o por carril: se agrupan las cercanas."""
    dist, at_km = project_points(route, booths["lat"], booths["lon"], match_radius_km)
    on = np.isfinite(dist)
    b = booths[on].copy()
    b["km"] = at_km[on]
    tolls = []
    for row in b.sort_values("km").itertuples():
        if tolls and row.km - tolls[-1]["km"] < merge_km:
            continue
        tolls.append({"km": row.km, "osm_id": row.osm_id, "nombre": row.nombre,
                      "operador": row.operador, "lat": row.lat, "lon": row.lon})
    return tolls
