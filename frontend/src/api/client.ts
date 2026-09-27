import type { Alerta, Camion, Lugar, PlanRequest, PlanResponse, ResumenAlertas } from './types'

const BASE = import.meta.env.VITE_API_URL ?? '/api'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...init?.headers },
    })
  } catch {
    throw new ApiError(0, 'No se pudo conectar con la API. ¿Está corriendo `uvicorn api.main:app`?')
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    const detail = body?.detail
    // Los errores de validación de FastAPI vienen como lista; los nuestros, como texto.
    const message = Array.isArray(detail)
      ? detail.map((d: { loc: string[]; msg: string }) => `${d.loc.at(-1)}: ${d.msg}`).join('; ')
      : (detail ?? `Error ${res.status}`)
    throw new ApiError(res.status, message)
  }
  return res.json() as Promise<T>
}

export const api = {
  lugares: (q: string) => request<Lugar[]>(`/lugares?q=${encodeURIComponent(q)}`),
  camiones: () => request<Camion[]>('/camiones'),
  planificar: (body: PlanRequest) => request<PlanResponse>('/planes', { method: 'POST', body: JSON.stringify(body) }),
  alertas: (params: { tipo?: string; camion_id?: string; limite?: number }) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined && v !== '').map(([k, v]) => [k, String(v)]),
    )
    return request<Alerta[]>(`/alertas?${qs}`)
  },
  resumenAlertas: () => request<ResumenAlertas>('/alertas/resumen'),
}
