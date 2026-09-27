import { useQuery } from '@tanstack/react-query'
import { useEffect, useId, useState } from 'react'
import { api } from '../api/client'
import { inputClass } from './ui'

function useDebounced<T>(value: T, ms: number) {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

/** Campo de localidad con sugerencias de /lugares (lugares del config + Georef). */
export function PlaceInput({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  const listId = useId()
  const q = useDebounced(value.trim(), 300)
  const { data } = useQuery({
    queryKey: ['lugares', q],
    queryFn: () => api.lugares(q),
    enabled: q.length >= 2,
    staleTime: 5 * 60_000,
  })
  return (
    <>
      <input
        className={inputClass}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        list={listId}
        placeholder={placeholder}
        autoComplete="off"
        required
      />
      <datalist id={listId}>
        {data?.map((l) => <option key={`${l.nombre}-${l.lat}`} value={l.nombre} />)}
      </datalist>
    </>
  )
}
