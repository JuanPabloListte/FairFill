"""Descarga y limpieza de las fuentes reales: estaciones y precios (Energía) y cabinas de peaje (OSM)."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ENERGIA_URL = (
    "http://datos.energia.gob.ar/dataset/1c181390-5045-475e-94dc-410429be4b17/resource/"
    "80ac25de-a44a-4445-9215-090cf55cfda5/download/precios-en-surtidor-resolucin-3142016.csv"
)
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_QUERY = """
[out:json][timeout:180];
area["ISO3166-1"="AR"][admin_level=2]->.a;
node["barrier"="toll_booth"](area.a);
out body;
"""
USER_AGENT = "fleet-sim/0.1 (proyecto educativo)"

# Caja envolvente amplia de Argentina continental, para descartar coordenadas mal cargadas.
AR_LAT = (-55.5, -21.5)
AR_LON = (-73.8, -53.3)


def _download(url: str, dest: Path, refresh: bool, **kwargs) -> Path:
    if dest.exists() and not refresh:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  descargando {dest.name} ...")
    method = kwargs.pop("method", "GET")
    resp = requests.request(method, url, timeout=300, headers={"User-Agent": USER_AGENT}, **kwargs)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


def fetch_raw(raw_dir: Path, refresh: bool = False) -> dict[str, Path]:
    return {
        "precios": _download(ENERGIA_URL, raw_dir / "precios_surtidor.csv", refresh),
        "peajes": _download(
            OVERPASS_URL, raw_dir / "osm_toll_booths.json", refresh,
            method="POST", data={"data": OVERPASS_QUERY},
        ),
    }


def load_stations(path: Path, fuel_cfg: dict, rng: np.random.Generator) -> pd.DataFrame:
    """Estaciones que venden el producto, con un precio base estimado para cada una.

    Muchas estaciones informan precios viejos (en septiembre de 2026, casi toda la red YPF).
    Si el precio propio es reciente se usa ese; si no, se estima con la mediana reciente de
    su bandera y provincia, cayendo a bandera, provincia o país si hay pocos datos.
    """
    df = pd.read_csv(path, low_memory=False)
    df = df[(df["producto"] == fuel_cfg["product"]) & (df["tipohorario"] == "Diurno")].copy()
    df = df.dropna(subset=["latitud", "longitud"])
    df = df[df["latitud"].between(*AR_LAT) & df["longitud"].between(*AR_LON)]
    df = df[df["precio"] > 500]  # hay registros con precios absurdos (< $500)
    df["fecha_vigencia"] = pd.to_datetime(df["fecha_vigencia"], errors="coerce")
    df = df.drop_duplicates(subset=["idempresa", "direccion"])

    cutoff = df["fecha_vigencia"].max() - pd.Timedelta(days=fuel_cfg["fresh_days"])
    df["precio_reciente"] = df["fecha_vigencia"] >= cutoff
    fresh = df[df["precio_reciente"]]
    min_n = fuel_cfg["min_group_size"]

    def medians(keys):
        g = fresh.groupby(keys)["precio"].agg(["median", "count"])
        return g[g["count"] >= min_n]["median"]

    by_bp = medians(["empresabandera", "provincia"])
    by_b = medians(["empresabandera"])
    by_p = medians(["provincia"])
    overall = float(fresh["precio"].median())

    def estimate(row):
        if row.precio_reciente:
            return row.precio, "propio_reciente"
        for series, key, label in (
            (by_bp, (row.empresabandera, row.provincia), "bandera_provincia"),
            (by_b, row.empresabandera, "bandera"),
            (by_p, row.provincia, "provincia"),
        ):
            if key in series.index:
                return float(series[key]), label
        return overall, "pais"

    est = [estimate(r) for r in df.itertuples()]
    df["precio_base"] = [e[0] for e in est]
    df["precio_base_fuente"] = [e[1] for e in est]
    # Dispersión propia de cada estación, estable durante toda la simulación.
    df["factor_estacion"] = rng.lognormal(0.0, fuel_cfg["station_price_sd"], len(df))

    out = df.rename(columns={
        "empresabandera": "bandera", "latitud": "lat", "longitud": "lon",
        "fecha_vigencia": "fecha_ultimo_informe",
    })[[
        "idempresa", "empresa", "bandera", "direccion", "localidad", "provincia", "lat", "lon",
        "precio", "fecha_ultimo_informe", "precio_base", "precio_base_fuente", "factor_estacion",
    ]].reset_index(drop=True)
    out.insert(0, "station_id", np.arange(len(out)))
    return out


def load_toll_booths(path: Path) -> pd.DataFrame:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = [
        {
            "osm_id": e["id"], "lat": e["lat"], "lon": e["lon"],
            "nombre": e.get("tags", {}).get("name"),
            "operador": e.get("tags", {}).get("operator"),
        }
        for e in data["elements"] if e.get("type") == "node"
    ]
    return pd.DataFrame(rows)
