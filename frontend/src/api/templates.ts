import type { RenderedTemplate, TemplateDetail, TemplateInput, TemplateVariables, TemplateVersionList } from '../types/templates'

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials: 'include', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) }, ...init })
  if (!response.ok) {
    const data = await response.json().catch(() => null) as { detail?: string | string[] } | null
    const detail = data?.detail
    throw new Error(Array.isArray(detail) ? detail.join('. ') : (detail ?? 'Template request failed'))
  }
  return response.json() as Promise<T>
}

export const templatesApi = {
  list: (token: string) => request<{ items: TemplateDetail[]; total: number }>('/templates', token),
  get: (id: string, token: string) => request<TemplateDetail>(`/templates/${id}`, token),
  create: (input: TemplateInput, token: string) => request<TemplateDetail>('/templates', token, { method: 'POST', body: JSON.stringify(input) }),
  update: (id: string, input: Partial<TemplateInput>, token: string) => request<TemplateDetail>(`/templates/${id}`, token, { method: 'PATCH', body: JSON.stringify(input) }),
  preview: (id: string, input: { recipient: Record<string, string>; sender: Record<string, string>; custom_values?: Record<string, string>; sender_id?: string }, token: string) => request<RenderedTemplate>(`/templates/${id}/preview`, token, { method: 'POST', body: JSON.stringify(input) }),
  recipientPreview: (id: string, contactId: string, token: string) => request<RenderedTemplate>(`/templates/${id}/preview/contact/${contactId}`, token, { method: 'POST', body: JSON.stringify({ sender: {} }) }),
  duplicate: (id: string, name: string, token: string) => request<TemplateDetail>(`/templates/${id}/duplicate?name=${encodeURIComponent(name)}`, token, { method: 'POST' }),
  versions: (id: string, token: string) => request<TemplateVersionList>(`/templates/${id}/versions`, token),
  variables: (token: string) => request<TemplateVariables>('/templates/variables', token),
  setStatus: (id: string, status: 'DRAFT' | 'ACTIVE' | 'ARCHIVED', token: string) => request<TemplateDetail>(`/templates/${id}/status?status=${status}`, token, { method: 'POST' }),
}