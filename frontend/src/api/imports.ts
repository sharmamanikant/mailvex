export type ImportPreview = { headers: string[]; mapping: Record<string, string>; rows: Record<string, string>[]; row_count: number }

export type DuplicatePolicy = 'SKIP' | 'UPDATE' | 'CREATE_NEW'

export type ImportJob = {
  id: string
  status: string
  filename: string
  file_type: string
  column_mapping: Record<string, string>
  duplicate_policy: DuplicatePolicy
  preview: ImportPreview | null
  total_rows: number
  processed_rows: number
  successful_rows: number
  failed_rows: number
  duplicate_rows: number
  suppressed_rows: number
  updated_rows: number
  error_message: string | null
  error_report_available: boolean
  counts: Record<string, unknown>
  created_at: string | null
  started_at: string | null
  finished_at: string | null
}

export type ImportJobUpdate = { column_mapping?: Record<string, string>; duplicate_policy?: DuplicatePolicy }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials: 'include', ...init })
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null
    throw new Error(payload?.detail ?? 'Import request failed')
  }
  return response.json() as Promise<T>
}

function upload<T>(path: string, file: File, token: string, onProgress?: (percent: number) => void): Promise<T> {
  return new Promise((resolve, reject) => {
    const body = new FormData()
    body.append('file', file)
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `/api/v1${path}`)
    xhr.withCredentials = true
    xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.upload.onprogress = (event) => {
      if (onProgress && event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100))
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(xhr.responseText ? (JSON.parse(xhr.responseText) as T) : (undefined as T))
        return
      }
      let detail = 'Import request failed'
      try {
        const payload = JSON.parse(xhr.responseText) as { detail?: string }
        if (payload?.detail) detail = payload.detail
      } catch {
        // fall back to the generic message
      }
      reject(new Error(detail))
    }
    xhr.onerror = () => reject(new Error('Import request failed'))
    xhr.send(body)
  })
}

export const importsApi = {
  preview: (file: File, token: string) =>
    upload<ImportPreview>('/contacts/imports/preview', file, token),
  create: (file: File, token: string, onProgress?: (percent: number) => void) =>
    upload<ImportJob>('/contacts/imports', file, token, onProgress),
  get: (id: string, token: string) => request<ImportJob>(`/contacts/imports/${id}`, { headers: { Authorization: `Bearer ${token}` } }),
  update: (id: string, input: ImportJobUpdate, token: string) => request<ImportJob>(`/contacts/imports/${id}`, { method: 'PUT', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: JSON.stringify(input) }),
  start: (id: string, token: string) => request<ImportJob>(`/contacts/imports/${id}/start`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } }),
  retry: (id: string, token: string) => request<ImportJob>(`/contacts/imports/${id}/retry`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } }),
  cancel: (id: string, token: string) => request<ImportJob>(`/contacts/imports/${id}/cancel`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } }),
  errorReportUrl: (id: string) => `/api/v1/contacts/imports/${id}/errors`,
}