import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { Card, ErrorBox } from '../components/ui'
import { litros, uno } from '../lib/format'

const FRIO: Record<string, string> = { tanque_propio: 'Tanque propio', tanque_principal: 'Del tanque principal' }

export function FleetPage() {
  const { data, error } = useQuery({ queryKey: ['camiones'], queryFn: api.camiones })
  if (error) return <ErrorBox error={error} />

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">Flota</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Consumo según la ficha técnica y el real, aprendido por el detector con los viajes de tanque lleno a tanque lleno.
          Se comparan a la carga típica de cada camión: el real incluye el efecto de sus choferes.
        </p>
      </div>
      <Card>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-sm">
            <thead className="text-left text-xs uppercase text-slate-500 dark:text-slate-400">
              <tr>
                <th className="py-2 pr-3">Camión</th><th className="pr-3">Tipo</th><th className="pr-3 text-right">Tanque</th>
                <th className="pr-3">Equipo de frío</th><th className="pr-3 text-right">Carga típica</th>
                <th className="pr-3 text-right">Ficha</th><th className="pr-3 text-right">Real</th><th className="text-right">Diferencia</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {data?.map((c) => {
                const ficha = c.consumo_ficha_carga_tipica_l100
                const real = c.consumo_real_carga_tipica_l100
                const diff = real !== null && ficha ? (real / ficha - 1) * 100 : null
                return (
                  <tr key={c.id}>
                    <td className="py-2 pr-3 font-medium">{c.id} <span className="font-normal text-slate-500">{c.patente}</span></td>
                    <td className="pr-3">{c.tipo.replaceAll('_', ' ')} · {c.ejes} ejes</td>
                    <td className="pr-3 text-right tabular-nums">{litros(c.tanque_l)}</td>
                    <td className="pr-3">{c.equipo_frio ? FRIO[c.equipo_frio] : '—'}</td>
                    <td className="pr-3 text-right tabular-nums">{c.carga_tipica_t !== null ? `${uno(c.carga_tipica_t)} t` : '—'}</td>
                    <td className="pr-3 text-right tabular-nums">{ficha !== null ? `${uno(ficha)} L/100 km` : '—'}</td>
                    <td className="pr-3 text-right tabular-nums">
                      {real !== null ? `${uno(real)} L/100 km` : (
                        <span className="text-slate-400" title={`${c.pares_modelo ?? 0} viajes tanque lleno a tanque lleno`}>
                          sin datos suficientes
                        </span>
                      )}
                    </td>
                    <td className={`text-right tabular-nums ${diff !== null && diff > 5 ? 'font-medium text-rose-700 dark:text-rose-400' : ''}`}>
                      {diff !== null ? `${diff > 0 ? '+' : ''}${uno(diff)}%` : '—'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}
