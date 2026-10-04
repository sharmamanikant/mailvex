import type { AIRequest, AIGeneration } from '../types/ai'

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials: 'include', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) }, ...init })
  if (!response.ok) { const data = await response.json().catch(() => null) as { detail?: string } | null; throw new Error(data?.detail ?? 'AI request failed') }
  return response.json() as Promise<T>
}
export const aiApi = {
  generate: (input: AIRequest, token: string) => request<AIGeneration>('/ai/generate-email', token, { method: 'POST', body: JSON.stringify(input) }),
  transform: (id: string, instruction: string, token: string, tone?: string, language?: string) => request<AIGeneration>(`/ai/generations/${id}/transform`, token, { method: 'POST', body: JSON.stringify({ instruction, tone, language }) }),
}
