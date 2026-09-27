"""Tests de la API sin red: estaciones, peajes y rutas falsos, y una corrida mínima en una carpeta temporal."""
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import planner.service as service
from api.main import create_app
from fleetsim.geo import Route, haversine_km
from planner.service import ROOT, PlannerContext, load_config

CBA = (-31.4167, -64.1833)
ROS = (-32.9468, -60.6393)


def _along(frac):
    return CBA[0] + (ROS[0] - CBA[0]) * frac, CBA[1] + (ROS[1] - CBA[1]) * frac


class FakeRouter:
    provider = "osrm"

    def route(self, origin, dest, spec, avoid_tolls=False):
        if avoid_tolls:
            raise RuntimeError("evitar peajes requiere ORS")
        km = float(haversine_km(origin["lat"], origin["lon"], dest["lat"], dest["lon"]))
        return Route.from_coords([[origin["lat"], origin["lon"]], [dest["lat"], dest["lon"]]], km / 70, "osrm")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_georef", lambda *a, **k: [])  # sin red
    stations = pd.DataFrame([{
        "station_id": i, "idempresa": 100 + i, "empresa": f"ESTACION {i}", "bandera": b,
        "direccion": f"Ruta 9 km {i}", "localidad": "LOCALIDAD", "provincia": "SANTA FE",
        "lat": _along(f)[0], "lon": _along(f)[1], "precio": p, "precio_base": p,
        "precio_base_fuente": src, "factor_estacion": 1.0,
    } for i, (f, b, p, src) in enumerate([
        (0.3, "YPF", 2300.0, "bandera"), (0.6, "SHELL C.A.P.S.A.", 2250.0, "propio_reciente"),
        (0.9, "YPF", 2400.0, "bandera")])])
    booths = pd.DataFrame([{"osm_id": 1, "lat": _along(0.5)[0], "lon": _along(0.5)[1],
                            "nombre": "Peaje de prueba", "operador": "Corredores Viales"}])
    ctx = PlannerContext(cfg=load_config(ROOT / "config.toml"), stations=stations, booths=booths, router=FakeRouter())

    pd.DataFrame([{
        "truck_id": "CAM-001", "patente": "ZZ123AB", "tipo": "semirremolque_5e", "ejes": 5, "categoria_peaje": "5-6E",
        "tanque_l": 600, "altura_m": 4.1, "peso_max_t": 45, "carga_util_kg": 28000,
        "consumo_ficha_vacio_l100": 28, "consumo_ficha_lleno_l100": 38, "chofer_titular": "CH-001",
        "tag_telepase": "TP-00001", "tarjeta_combustible": "TF-00001", "equipo_frio": "",
        "tanque_frio_l": 0, "consumo_frio_lh": 0,
    }]).to_csv(tmp_path / "trucks.csv", index=False)
    pd.DataFrame([
        {"alert_id": "AL00001", "truck_id": "CAM-001", "ts": "2026-06-10 10:00", "tipo": "carga_repetida",
         "tx_id": "TX000001", "litros_en_riesgo": 120, "ars_en_riesgo": 270000, "explicacion": "segunda carga"},
        {"alert_id": "AL00002", "truck_id": "CAM-001", "ts": "2026-06-12 03:00", "tipo": "caida_con_motor_apagado",
         "tx_id": None, "litros_en_riesgo": 60, "ars_en_riesgo": 135000, "explicacion": "bajó el nivel"},
    ]).to_csv(tmp_path / "alerts.csv", index=False)

    with TestClient(create_app(context=ctx, run_dir=tmp_path)) as c:
        yield c


def test_salud(client):
    r = client.get("/salud").json()
    assert r["estado"] == "ok" and r["estaciones"] == 3 and r["corrida_disponible"]


def test_camiones(client):
    r = client.get("/camiones").json()
    assert [c["id"] for c in r] == ["CAM-001"] and r[0]["equipo_frio"] is None
    assert client.get("/camiones/CAM-999").status_code == 404


def test_plan_factible_con_parada_y_peaje(client):
    r = client.post("/planes", json={"origen": "Córdoba", "destino": "Rosario", "camion_id": "CAM-001",
                                     "carga_kg": 25000, "litros_iniciales": 120, "salida": "2026-09-28T06:00"})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["factible"] and len(plan["paradas"]) >= 1 and len(plan["peajes"]) == 1
    # Sale un lunes a las 6 y cruza el peaje a ~200 km cerca de las 8:50: hora pico (7 a 10), categoría 5-6E.
    assert plan["peajes"][0]["pico"] and plan["peajes"][0]["importe"] == 6000
    assert plan["ruta"]["geometria"][0] == pytest.approx(list(CBA), abs=1e-4)
    assert plan["ruta"]["geometria"][-1] == pytest.approx(list(ROS), abs=1e-4)
    assert plan["costos"]["llega_con_l"] >= 0.15 * 600 - 1
    assert any("perfil de auto" in a for a in plan["avisos"])


def test_plan_prefiere_la_estacion_barata_confirmada(client):
    plan = client.post("/planes", json={"origen": "Córdoba", "destino": "Rosario", "camion_id": "CAM-001",
                                        "litros_iniciales": 200}).json()
    assert [p["estacion"] for p in plan["paradas"]] == ["ESTACION 1"]
    assert plan["paradas"][0]["precio_confirmado"]


def test_plan_errores(client):
    base = {"origen": "Córdoba", "destino": "Rosario", "camion_id": "CAM-001"}
    assert client.post("/planes", json={**base, "litros_iniciales": 900}).status_code == 422
    assert client.post("/planes", json={**base, "camion_id": "CAM-999"}).status_code == 404
    assert client.post("/planes", json={**base, "destino": "Lugar Inexistente"}).status_code == 422
    assert client.post("/planes", json={**base, "evitar_peajes": True}).status_code == 502
    assert client.post("/planes", json={**base, "carga_kg": -1}).status_code == 422


def test_plan_sin_estaciones_de_la_bandera(client):
    plan = client.post("/planes", json={"origen": "Córdoba", "destino": "Rosario", "camion_id": "CAM-001",
                                        "litros_iniciales": 100, "solo_bandera": "AXION"}).json()
    assert not plan["factible"] and plan["costos"] is None


def test_alertas_y_resumen(client):
    todas = client.get("/alertas").json()
    assert [a["alert_id"] for a in todas] == ["AL00002", "AL00001"]  # más recientes primero
    assert todas[0]["tx_id"] is None
    solo = client.get("/alertas", params={"tipo": "carga_repetida"}).json()
    assert len(solo) == 1 and solo[0]["tx_id"] == "TX000001"
    resumen = client.get("/alertas/resumen").json()
    assert resumen["total"] == 2 and resumen["ars_en_riesgo"] == 405000


def test_lugares_usa_los_del_config(client):
    assert [x["nombre"] for x in client.get("/lugares", params={"q": "rosa"}).json()] == ["Rosario"]
