import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../api/client'
import { Card, ErrorBox, Stat, inputClass } from '../components/ui'
import { TIPOS_ALERTA, etiquetaAlerta, fechaHora, litros, pesos } from '../lib/format'

export function AlertsPage() {
  const [tipo, setTipo] = useState('')
  const [camion, setCamion] = useState('')
  const resumen = useQuery({ queryKey: ['alertas-resumen'], queryFn: api.resumenAlertas })
  const alertas = useQuery({
    queryKey: ['alertas', tipo, camion],
    queryFn: () => api.alertas({ tipo, camion_id: camion, limite: 500 }),
  })

  if (resumen.error) return <ErrorBox error={resumen.error} />
  const r = resumen.data
  const fraude = r ? Object.entries(r.por_tipo).filter(([t]) => TIPOS_ALERTA[t]?.fraude !== false).reduce((s, [, n]) => s + n, 0) : 0

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold">Control de cargas</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Cargas que no cierran al cruzar la tarjeta de combustible con el GPS y el sensor de nivel. Son para revisar, no acusaciones.
        </p>
      </div>

      {r && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat label="Alertas" value={String(r.total)} hint={`${fraude} posibles fraudes`} />
          <Stat label="En riesgo" value={pesos(r.ars_en_riesgo)} tone="bad" />
          <Stat label="Tipo más frecuente" value={etiquetaAlerta(Object.keys(r.por_tipo)[0] ?? '—')}
            hint={`${Object.values(r.por_tipo)[0] ?? 0} alertas`} />
          <Stat label="Camión con más alertas" value={Object.keys(r.por_camion)[0] ?? '—'}
            hint={`${Object.values(r.por_camion)[0] ?? 0} alertas`} />
        </div>
      )}

      <Card>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-sm">
            <span className="font-medium">Tipo</span>
            <select className={`${inputClass} mt-1 min-w-56`} value={tipo} onChange={(e) => setTipo(e.target.value)}>
              <option value="">Todos</option>
              {Object.entries(r?.por_tipo ?? {}).map(([t, n]) => <option key={t} value={t}>{etiquetaAlerta(t)} ({n})</option>)}
            </select>
          </label>
          <label className="text-sm">
            <span className="font-medium">Camión</span>
            <select className={`${inputClass} mt-1 min-w-40`} value={camion} onChange={(e) => setCamion(e.target.value)}>
              <option value="">Todos</option>
              {Object.keys(r?.por_camion ?? {}).sort().map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>
          <span className="ml-auto text-sm text-slate-500">{alertas.data?.length ?? 0} resultados</span>
        </div>

        {alertas.error && <div className="mt-3"><ErrorBox error={alertas.error} /></div>}
        <ul className="mt-4 divide-y divide-slate-100 dark:divide-slate-800">
          {alertas.data?.map((a) => {
            const fraude = TIPOS_ALERTA[a.tipo]?.fraude !== false
            return (
              <li key={a.alert_id} className="grid gap-1 py-3 md:grid-cols-[150px_110px_1fr_auto] md:items-baseline md:gap-4">
                <span className="text-sm tabular-nums text-slate-500">{fechaHora(a.ts)}</span>
                <span className="text-sm font-medium">{a.truck_id}</span>
                <div>
                  <span className={`mr-2 inline-block rounded px-2 py-0.5 text-xs font-medium ${
                    fraude ? 'bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300'
                      : 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300'}`}>
                    {etiquetaAlerta(a.tipo)}
                  </span>
                  <span className="text-sm">{a.explicacion}</span>
                </div>
                <span className="text-sm tabular-nums md:text-right">
                  {a.ars_en_riesgo > 0 ? `${pesos(a.ars_en_riesgo)} · ${litros(a.litros_en_riesgo)}` : 'procedimiento'}
                </span>
              </li>
            )
          })}
        </ul>
      </Card>
    </div>
  )
}
