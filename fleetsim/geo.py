"""Utilidades geográficas: distancias, rutas remuestreadas y proyección de puntos sobre la ruta."""
from dataclasses import dataclass

import numpy as np

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


@dataclass
class Route:
    """Ruta remuestreada a paso fijo, para interpolar posiciones por km recorrido."""
    lat: np.ndarray
    lon: np.ndarray
    km: np.ndarray          # km acumulados en cada punto
    duration_h: float       # duración estimada por el ruteador (ya ajustada para camión)
    source: str

    @property
    def length_km(self) -> float:
        return float(self.km[-1])

    def position_at(self, km: float) -> tuple[float, float]:
        return float(np.interp(km, self.km, self.lat)), float(np.interp(km, self.km, self.lon))

    @classmethod
    def from_coords(cls, coords_latlon, duration_h, source, step_km=0.25):
        pts = np.asarray(coords_latlon, dtype=float)
        seg = haversine_km(pts[:-1, 0], pts[:-1, 1], pts[1:, 0], pts[1:, 1])
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        # Descarta puntos repetidos para que np.interp reciba km estrictamente crecientes.
        keep = np.concatenate([[True], np.diff(cum) > 1e-9])
        pts, cum = pts[keep], cum[keep]
        grid = np.arange(0.0, cum[-1], step_km)
        grid = np.append(grid, cum[-1])
        return cls(
            lat=np.interp(grid, cum, pts[:, 0]),
            lon=np.interp(grid, cum, pts[:, 1]),
            km=grid,
            duration_h=duration_h,
            source=source,
        )


def project_points(route: Route, lat, lon, max_dist_km: float):
    """Para cada punto devuelve (distancia mínima a la ruta, km de la ruta donde ocurre).

    Usa proyección equirectangular local; con rutas remuestreadas cada 250 m el error es
    despreciable para decidir si una estación o cabina está "sobre" la ruta.
    """
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    dist = np.full(lat.shape, np.inf)
    at_km = np.full(lat.shape, np.nan)
    if lat.size == 0:
        return dist, at_km

    # Filtro grueso por caja envolvente antes de calcular distancias.
    pad = max_dist_km / 100.0 + 0.01
    inside = (
        (lat >= route.lat.min() - pad) & (lat <= route.lat.max() + pad)
        & (lon >= route.lon.min() - pad) & (lon <= route.lon.max() + pad)
    )
    idx = np.flatnonzero(inside)
    if idx.size == 0:
        return dist, at_km

    lat0 = np.radians(route.lat.mean())
    rx = np.radians(route.lon) * np.cos(lat0) * EARTH_RADIUS_KM
    ry = np.radians(route.lat) * EARTH_RADIUS_KM
    for chunk in np.array_split(idx, max(1, idx.size // 500)):
        px = np.radians(lon[chunk])[:, None] * np.cos(lat0) * EARTH_RADIUS_KM
        py = np.radians(lat[chunk])[:, None] * EARTH_RADIUS_KM
        d = np.hypot(px - rx[None, :], py - ry[None, :])
        j = d.argmin(axis=1)
        dist[chunk] = d[np.arange(chunk.size), j]
        at_km[chunk] = route.km[j]
    far = dist > max_dist_km
    dist[far] = np.inf
    at_km[far] = np.nan
    return dist, at_km
