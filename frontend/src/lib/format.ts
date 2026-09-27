const ars = new Intl.NumberFormat('es-AR', { style: 'currency', currency: 'ARS', maximumFractionDigits: 0 })
const num = new Intl.NumberFormat('es-AR', { maximumFractionDigits: 0 })
const dec = new Intl.NumberFormat('es-AR', { maximumFractionDigits: 1 })
const dateTime = new Intl.DateTimeFormat('es-AR', {
  day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})

export const pesos = (v: number) => ars.format(v)
export const litros = (v: number) => `${num.format(v)} L`
export const km = (v: number) => `${num.format(v)} km`
export const uno = (v: number) => dec.format(v)
export const fechaHora = (iso: string) => dateTime.format(new Date(iso))

export const horas = (h: number) => {
  const total = Math.round(h * 60)
  return `${Math.floor(total / 60)} h ${String(total % 60).padStart(2, '0')} min`
}

// Valor para <input type="datetime-local">: mañana a las 6.
export const mananaALasSeis = () => {
  const d = new Date()
  d.setDate(d.getDate() + 1)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}T06:00`
}

export const TIPOS_ALERTA: Record<string, { label: string; fraude: boolean }> = {
  tarjeta_lejos_del_camion: { label: 'Tarjeta lejos del camión', fraude: true },
  litros_no_reflejados: { label: 'Litros que no entraron al tanque', fraude: true },
  carga_repetida: { label: 'Carga repetida', fraude: true },
  carga_mayor_al_consumo: { label: 'Carga mayor al consumo', fraude: true },
  caida_con_motor_apagado: { label: 'Caída con motor apagado', fraude: true },
  tarjeta_compartida: { label: 'Tarjeta compartida', fraude: false },
}

export const etiquetaAlerta = (tipo: string) => TIPOS_ALERTA[tipo]?.label ?? tipo
