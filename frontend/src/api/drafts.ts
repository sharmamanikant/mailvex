export type AIMessageDraft = {
  id: string
  tenant_id: string
  created_by_id: string | null
  contact_id: string | null
  campaign_id: string | null
  template_id: string | null
  approved_template_id: string | null
  objective: string
  input_context: Record<string, unknown>
  generated_subject: string
  generated_body: string
  generation_status: string
  generation_type: string
  tone: string
  language: string
  desired_length: string
  cta: string
  provider: string
  model: string | null
  generation_method: string
  warnings: string[]
  input_tokens: number | null
  output_tokens: number | null
  total_tokens: number | null
  estimated_cost: string | null
  cost_currency: string
  request_duration_ms: number | null
  error_message: string | null
  review_note: string | null
  reviewed_at: string | null
  approved_at: string | null
  approved_by_id: string | null
  created_at: string
  updated_at: string
}

export type DraftGenerateInput = {
  objective: string
  audience?: string
  context?: string
  service?: string
  product?: string
  cta?: string
  tone?: string
  language?: string
  desired_length?: string
  generation_type?: string
  sender?: Record<string, string>
  recipient?: Record<string, string>
  custom_values?: Record<string, string>
  sender_id?: string
  contact_id?: string
  template_id?: string
}

export type DraftTransformInput = {
  action: string
  tone?: string
  language?: string
}

export type DraftUsage = {
  provider: string | null
  model: string | null
  monthly_generations: number
  monthly_cost_usd: string
  monthly_budget_usd: string
  monthly_generation_limit: number
}

type Detail = { detail?: string | { msg?: string } }
function message(data: Detail | null, fallback: string): string {
  const detail = data?.detail
  return typeof detail === 'string' ? detail : detail?.msg ?? fallback
}

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials: 'include', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) }, ...init })
  if (!response.ok) {
    const data = await response.json().catch(() => null) as Detail | null
    throw new Error(message(data, 'AI draft request failed'))
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const draftsApi = {
  create: (input: DraftGenerateInput, token: string) => request<AIMessageDraft>('/ai/drafts', token, { method: 'POST', body: JSON.stringify(input) }),
  get: (id: string, token: string) => request<AIMessageDraft>(`/ai/drafts/${id}`, token),
  list: (token: string) => request<{ items: AIMessageDraft[]; total: number }>('/ai/drafts', token),
  usage: (token: string) => request<DraftUsage>('/ai/drafts/usage', token),
  update: (id: string, input: { subject?: string; body?: string }, token: string) => request<AIMessageDraft>(`/ai/drafts/${id}`, token, { method: 'PATCH', body: JSON.stringify(input) }),
  transform: (id: string, input: DraftTransformInput, token: string) => request<AIMessageDraft>(`/ai/drafts/${id}/transform`, token, { method: 'POST', body: JSON.stringify(input) }),
  approve: (id: string, note: string, token: string) => request<AIMessageDraft>(`/ai/drafts/${id}/approve`, token, { method: 'POST', body: JSON.stringify({ note }) }),
  reject: (id: string, note: string, token: string) => request<AIMessageDraft>(`/ai/drafts/${id}/reject`, token, { method: 'POST', body: JSON.stringify({ note }) }),
  saveAsTemplate: (id: string, templateName: string, token: string) => request<AIMessageDraft>(`/ai/drafts/${id}/save-as-template`, token, { method: 'POST', body: JSON.stringify({ template_name: templateName }) }),
}