"""Contratos de la API: lo que entra y lo que sale de cada endpoint."""
from datetime import datetime

from pydantic import BaseModel, Field


class Lugar(BaseModel):
    nombre: str
    lat: float
    lon: float


class Camion(BaseModel):
    id: str
    patente: str
    tipo: str
    ejes: int
    categoria_peaje: str
    tanque_l: float
    carga_util_kg: float
    equipo_frio: str | None = Field(description="tanque_propio, tanque_principal o nulo")
    consumo_ficha_vacio_l100: float
    consumo_ficha_lleno_l100: float
    carga_tipica_t: float | None = Field(None, description="carga promedio por km recorrido en la corrida")
    consumo_ficha_carga_tipica_l100: float | None = None
    consumo_real_carga_tipica_l100: float | None = Field(
        None, description="modelo del detector evaluado a la carga típica; incluye el efecto del chofer")
    pares_modelo: int | None = Field(None, description="viajes tanque lleno a tanque lleno usados para ajustar el modelo")


class PlanRequest(BaseModel):
    origen: str = Field(examples=["Córdoba"])
    destino: str = Field(examples=["Buenos Aires"])
    camion_id: str = Field(examples=["CAM-001"])
    carga_kg: float = Field(0.0, ge=0, examples=[25000])
    litros_iniciales: float | None = Field(None, ge=0, description="por defecto, medio tanque")
    salida: datetime | None = Field(None, description="por defecto, mañana a las 6")
    solo_bandera: str | None = Field(None, description="solo estaciones de esta bandera, p. ej. por contrato")
    evitar_peajes: bool = Field(False, description="requiere ORS_API_KEY en el servidor")


class Parada(BaseModel):
    km: float
    hora: datetime
    estacion: str
    bandera: str
    direccion: str
    localidad: str
    desvio_km: float
    llega_con_l: float
    cargar_l: float
    precio: float = Field(description="$/L esperado, con el descuento de la tarjeta si corresponde")
    precio_confirmado: bool = Field(description="falso si la estación no informa su precio hace más de 60 días")
    lat: float
    lon: float


class Peaje(BaseModel):
    km: float
    hora: datetime
    nombre: str | None
    operador: str | None
    importe: float
    pico: bool
    lat: float
    lon: float


class Ruta(BaseModel):
    km: float
    horas_manejo: float
    fuente: str = Field(description="ors (perfil camión) u osrm (perfil auto)")
    geometria: list[tuple[float, float]] = Field(description="[lat, lon] simplificada")


class Costos(BaseModel):
    combustible: float = Field(description="lo que se paga en las paradas")
    costo_paradas: float = Field(description="tiempo y desvío de las paradas, valuados en pesos")
    variacion_tanque: float = Field(description="valor de lo que cambió el tanque entre salida y llegada")
    total_viaje: float = Field(description="costo del combustible consumido + paradas")
    peajes: float
    litros: float
    llega_con_l: float


class Comparacion(BaseModel):
    politica: str
    total_viaje: float
    paradas: int
    ahorro: float
    ahorro_pct: float


class CamionPlan(BaseModel):
    id: str
    patente: str
    tipo: str
    tanque_l: float
    consumo_l_100km: float
    consumo_frio_lh: float
    fuente_consumo: str


class PlanResponse(BaseModel):
    factible: bool
    origen: Lugar
    destino: Lugar
    camion: CamionPlan
    ruta: Ruta
    sale_con_l: float
    paradas: list[Parada]
    peajes: list[Peaje]
    costos: Costos | None
    comparacion: Comparacion | None
    avisos: list[str]


class Alerta(BaseModel):
    alert_id: str
    truck_id: str
    ts: datetime
    tipo: str
    tx_id: str | None
    litros_en_riesgo: float
    ars_en_riesgo: float
    explicacion: str


class ResumenAlertas(BaseModel):
    total: int
    ars_en_riesgo: float
    por_tipo: dict[str, int]
    por_camion: dict[str, int]


class Salud(BaseModel):
    estado: str
    proveedor_rutas: str
    estaciones: int
    cabinas_peaje: int
    corrida: str
    corrida_disponible: bool
