// Tipos que reflejan los esquemas de api/schemas.py.

export type Lugar = { nombre: string; lat: number; lon: number }

export type Camion = {
  id: string
  patente: string
  tipo: string
  ejes: number
  categoria_peaje: string
  tanque_l: number
  carga_util_kg: number
  equipo_frio: 'tanque_propio' | 'tanque_principal' | null
  consumo_ficha_vacio_l100: number
  consumo_ficha_lleno_l100: number
  carga_tipica_t: number | null
  consumo_ficha_carga_tipica_l100: number | null
  consumo_real_carga_tipica_l100: number | null
  pares_modelo: number | null
}

export type PlanRequest = {
  origen: string
  destino: string
  camion_id: string
  carga_kg: number
  litros_iniciales?: number | null
  salida?: string | null
  solo_bandera?: string | null
  evitar_peajes?: boolean
}

export type Parada = {
  km: number
  hora: string
  estacion: string
  bandera: string
  direccion: string
  localidad: string
  desvio_km: number
  llega_con_l: number
  cargar_l: number
  precio: number
  precio_confirmado: boolean
  lat: number
  lon: number
}

export type Peaje = {
  km: number
  hora: string
  nombre: string | null
  operador: string | null
  importe: number
  pico: boolean
  lat: number
  lon: number
}

export type Costos = {
  combustible: number
  costo_paradas: number
  variacion_tanque: number
  total_viaje: number
  peajes: number
  litros: number
  llega_con_l: number
}

export type Comparacion = {
  politica: string
  total_viaje: number
  paradas: number
  ahorro: number
  ahorro_pct: number
}

export type PlanResponse = {
  factible: boolean
  origen: Lugar
  destino: Lugar
  camion: {
    id: string
    patente: string
    tipo: string
    tanque_l: number
    consumo_l_100km: number
    consumo_frio_lh: number
    fuente_consumo: string
  }
  ruta: { km: number; horas_manejo: number; fuente: string; geometria: [number, number][] }
  sale_con_l: number
  paradas: Parada[]
  peajes: Peaje[]
  costos: Costos | null
  comparacion: Comparacion | null
  avisos: string[]
}

export type Alerta = {
  alert_id: string
  truck_id: string
  ts: string
  tipo: string
  tx_id: string | null
  litros_en_riesgo: number
  ars_en_riesgo: number
  explicacion: string
}

export type ResumenAlertas = {
  total: number
  ars_en_riesgo: number
  por_tipo: Record<string, number>
  por_camion: Record<string, number>
}
