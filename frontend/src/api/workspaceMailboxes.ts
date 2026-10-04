import type { ProviderConnection } from '../types/providerConnections'
import type { MailboxPage, WorkspaceMailbox } from '../types/workspaceMailboxes'

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) },
    ...init,
  })
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null
    throw new Error(payload?.detail ?? 'Mailbox request failed')
  }
  return response.json() as Promise<T>
}

export const workspaceMailboxesApi = {
  sync: (connectionId: string, token: string) =>
    request<ProviderConnection>(`/provider-connections/${connectionId}/sync`, token, { method: 'POST' }),
  list: (connectionId: string, params: URLSearchParams, token: string) =>
    request<MailboxPage>(`/provider-connections/${connectionId}/mailboxes?${params}`, token),
  get: (mailboxId: string, token: string) =>
    request<WorkspaceMailbox>(`/mailboxes/${mailboxId}`, token),
}