import type { LatLngBoundsExpression } from 'leaflet'
import { useEffect } from 'react'
import { CircleMarker, MapContainer, Polyline, Popup, TileLayer, useMap } from 'react-leaflet'
import type { PlanResponse } from '../api/types'
import { fechaHora, litros, pesos } from '../lib/format'

const ARGENTINA: LatLngBoundsExpression = [[-55, -73.5], [-21.8, -53.6]]

function FitRoute({ plan }: { plan: PlanResponse | null }) {
  const map = useMap()
  useEffect(() => {
    if (plan?.ruta.geometria.length) map.fitBounds(plan.ruta.geometria, { padding: [30, 30] })
  }, [plan, map])
  return null
}

/** Ruta, paradas (verde: precio confirmado; naranja: estimado), peajes (violeta), origen y destino. */
export function TripMap({ plan }: { plan: PlanResponse | null }) {
  return (
    <MapContainer bounds={ARGENTINA} className="h-[420px] w-full rounded-xl" scrollWheelZoom>
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
      />
      <FitRoute plan={plan} />
      {plan && (
        <>
          <Polyline positions={plan.ruta.geometria} pathOptions={{ color: '#2563eb', weight: 4 }} />
          {plan.peajes.map((t) => (
            <CircleMarker key={`${t.km}`} center={[t.lat, t.lon]} radius={5} pathOptions={{ color: '#7c3aed', fillOpacity: 0.8 }}>
              <Popup>
                {t.nombre ?? 'Peaje'} · {t.operador ?? 'operador desconocido'}
                <br />
                {pesos(t.importe)} {t.pico && '(hora pico)'} · {fechaHora(t.hora)}
              </Popup>
            </CircleMarker>
          ))}
          {plan.paradas.map((p, i) => (
            <CircleMarker
              key={`${p.km}`}
              center={[p.lat, p.lon]}
              radius={10}
              pathOptions={{ color: p.precio_confirmado ? '#059669' : '#d97706', fillOpacity: 0.9 }}
            >
              <Popup>
                <strong>Parada {i + 1}</strong>: {p.estacion} ({p.bandera})
                <br />
                Cargar {litros(p.cargar_l)} a {pesos(p.precio)}/L {p.precio_confirmado ? '' : '(estimado)'}
                <br />
                {fechaHora(p.hora)}
              </Popup>
            </CircleMarker>
          ))}
          {[plan.origen, plan.destino].map((l, i) => (
            <CircleMarker key={i} center={[l.lat, l.lon]} radius={7} pathOptions={{ color: '#0f172a', fillColor: '#fff', fillOpacity: 1, weight: 3 }}>
              <Popup>{i === 0 ? 'Origen' : 'Destino'}: {l.nombre}</Popup>
            </CircleMarker>
          ))}
        </>
      )}
    </MapContainer>
  )
}
