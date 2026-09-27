import { useMutation, useQuery } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { api } from '../api/client'
import type { PlanRequest, PlanResponse } from '../api/types'
import { PlaceInput } from '../components/PlaceInput'
import { TripMap } from '../components/TripMap'
import { Card, ErrorBox, Field, Stat, inputClass } from '../components/ui'
import { fechaHora, horas, km, litros, mananaALasSeis, pesos, uno } from '../lib/format'

type Form = {
  origen: string
  destino: string
  camion_id: string
  carga_kg: string
  litros_iniciales: string
  salida: string
  solo_bandera: string
}

export function PlanPage() {
  const camiones = useQuery({ queryKey: ['camiones'], queryFn: api.camiones })
  const [form, setForm] = useState<Form>({
    origen: 'Córdoba', destino: 'Buenos Aires', camion_id: '', carga_kg: '25000',
    litros_iniciales: '', salida: mananaALasSeis(), solo_bandera: '',
  })
  const plan = useMutation({ mutationFn: (body: PlanRequest) => api.planificar(body) })
  const set = (k: keyof Form) => (v: string) => setForm((f) => ({ ...f, [k]: v }))
  const camionId = form.camion_id || camiones.data?.[0]?.id || ''
  const camion = camiones.data?.find((c) => c.id === camionId)

  function submit(e: FormEvent) {
    e.preventDefault()
    plan.mutate({
      origen: form.origen,
      destino: form.destino,
      camion_id: camionId,
      carga_kg: Number(form.carga_kg) || 0,
      litros_iniciales: form.litros_iniciales === '' ? null : Number(form.litros_iniciales),
      salida: form.salida || null,
      solo_bandera: form.solo_bandera || null,
    })
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[340px_1fr]">
      <Card className="h-fit">
        <h1 className="text-lg font-semibold">Planificar un viaje</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">Dónde y cuánto cargar para gastar lo menos posible.</p>
        <form onSubmit={submit} className="mt-4 space-y-3">
          <Field label="Origen"><PlaceInput value={form.origen} onChange={set('origen')} placeholder="Localidad, Provincia" /></Field>
          <Field label="Destino"><PlaceInput value={form.destino} onChange={set('destino')} placeholder="Localidad, Provincia" /></Field>
          <Field label="Camión">
            <select className={inputClass} value={camionId} onChange={(e) => set('camion_id')(e.target.value)} required>
              {camiones.data?.map((c) => (
                <option key={c.id} value={c.id}>{c.id} · {c.patente} · {c.tipo.replaceAll('_', ' ')}</option>
              ))}
            </select>
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Carga (kg)">
              <input className={inputClass} type="number" min={0} max={camion?.carga_util_kg} step={500}
                value={form.carga_kg} onChange={(e) => set('carga_kg')(e.target.value)} />
            </Field>
            <Field label="Sale con (L)" hint={camion ? `Tanque de ${litros(camion.tanque_l)}` : undefined}>
              <input className={inputClass} type="number" min={0} max={camion?.tanque_l} placeholder="medio tanque"
                value={form.litros_iniciales} onChange={(e) => set('litros_iniciales')(e.target.value)} />
            </Field>
          </div>
          <Field label="Salida">
            <input className={inputClass} type="datetime-local" value={form.salida} onChange={(e) => set('salida')(e.target.value)} />
          </Field>
          <Field label="Solo esta bandera" hint="Opcional, por ejemplo por contrato con una petrolera">
            <select className={inputClass} value={form.solo_bandera} onChange={(e) => set('solo_bandera')(e.target.value)}>
              <option value="">Cualquiera</option>
              {['YPF', 'SHELL', 'AXION', 'PUMA', 'GULF'].map((b) => <option key={b} value={b}>{b}</option>)}
            </select>
          </Field>
          <button
            type="submit"
            disabled={plan.isPending || !camionId}
            className="w-full rounded-md bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            {plan.isPending ? 'Calculando…' : 'Planificar'}
          </button>
        </form>
        {camiones.error && <div className="mt-3"><ErrorBox error={camiones.error} /></div>}
      </Card>

      <div className="min-w-0 space-y-4">
        <TripMap plan={plan.data ?? null} />
        {plan.error && <ErrorBox error={plan.error} />}
        {plan.data && <PlanResult plan={plan.data} />}
        {!plan.data && !plan.error && (
          <p className="text-sm text-slate-500 dark:text-slate-400">Completá el formulario para ver la ruta, las paradas y los costos.</p>
        )}
      </div>
    </div>
  )
}

function PlanResult({ plan }: { plan: PlanResponse }) {
  const { costos, comparacion } = plan
  return (
    <>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Recorrido" value={km(plan.ruta.km)} hint={`${horas(plan.ruta.horas_manejo)} de manejo`} />
        <Stat label="Combustible" value={costos ? pesos(costos.combustible) : '—'}
          hint={costos ? `${litros(costos.litros)} en ${plan.paradas.length} parada(s)` : undefined} />
        <Stat label="Peajes" value={costos ? pesos(costos.peajes) : '—'} hint={`${plan.peajes.length} cabinas`} />
        <Stat label="Ahorro estimado" tone={comparacion && comparacion.ahorro > 0 ? 'good' : undefined}
          value={comparacion ? pesos(comparacion.ahorro) : '—'}
          hint={comparacion ? `${uno(comparacion.ahorro_pct)}% frente a ${comparacion.politica}` : undefined} />
      </div>

      {plan.avisos.length > 0 && (
        <ul className="space-y-1 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          {plan.avisos.map((a) => <li key={a}>{a}</li>)}
        </ul>
      )}

      {plan.factible && (
        <Card>
          <h2 className="font-semibold">Paradas</h2>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            {plan.camion.id} consume {uno(plan.camion.consumo_l_100km)} L/100 km según {plan.camion.fuente_consumo}.
            Sale con {litros(plan.sale_con_l)} y llega con {costos ? litros(costos.llega_con_l) : '—'}.
          </p>
          {plan.paradas.length === 0 ? (
            <p className="mt-3 text-sm">No hace falta cargar: llega con lo que tiene.</p>
          ) : (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full min-w-[640px] text-sm">
                <thead className="text-left text-xs uppercase text-slate-500 dark:text-slate-400">
                  <tr>
                    <th className="py-2 pr-3">#</th><th className="pr-3">Hora</th><th className="pr-3">Estación</th>
                    <th className="pr-3">Localidad</th><th className="pr-3 text-right">Km</th>
                    <th className="pr-3 text-right">Llega con</th><th className="pr-3 text-right">Cargar</th><th className="text-right">Precio</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                  {plan.paradas.map((p, i) => (
                    <tr key={p.km}>
                      <td className="py-2 pr-3 font-medium">{i + 1}</td>
                      <td className="pr-3 whitespace-nowrap">{fechaHora(p.hora)}</td>
                      <td className="pr-3">{p.estacion} <span className="text-slate-500">({p.bandera})</span></td>
                      <td className="pr-3">{p.localidad}</td>
                      <td className="pr-3 text-right tabular-nums">{km(p.km)}</td>
                      <td className="pr-3 text-right tabular-nums">{litros(p.llega_con_l)}</td>
                      <td className="pr-3 text-right font-semibold tabular-nums">{litros(p.cargar_l)}</td>
                      <td className={`text-right tabular-nums ${p.precio_confirmado ? '' : 'text-amber-700 dark:text-amber-400'}`}>
                        {pesos(p.precio)}{p.precio_confirmado ? '' : ' est.'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </>
  )
}
