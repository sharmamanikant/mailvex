import type {
  CredentialStatus,
  CredentialUpload,
  DiscoveryResponse,
  GoogleAuthorizeRequest,
  GoogleAuthorizeResponse,
  IntegrationCreate,
  MicrosoftAuthorizeRequest,
  MicrosoftAuthorizeResponse,
  MicrosoftConnectionDetails,
  MicrosoftTestSendRequest,
  ProviderCapability,
  SenderAccount,
  SenderConnection,
  SenderImportRequest,
  SenderImportResponse,
  TestSendRequest,
  TestSendResponse,
} from '../types/integrations'

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) },
    ...init,
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(payload?.detail ?? 'Integration request failed')
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const providersApi = {
  list: (token: string) => request<ProviderCapability[]>('/providers', token),
}

export const senderConnectionsApi = {
  list: (token: string) => request<SenderConnection[]>('/sender-connections', token),
  create: (input: IntegrationCreate, token: string) => request<SenderConnection>('/sender-connections', token, { method: 'POST', body: JSON.stringify(input) }),
  get: (id: string, token: string) => request<SenderConnection>(`/sender-connections/${id}`, token),
  validate: (id: string, token: string) => request<SenderConnection>(`/sender-connections/${id}/validate`, token, { method: 'POST' }),
  disconnect: (id: string, token: string) => request<SenderConnection>(`/sender-connections/${id}/disconnect`, token, { method: 'POST' }),
  discover: (id: string, token: string) => request<DiscoveryResponse>(`/sender-connections/${id}/discover`, token, { method: 'POST' }),
  delete: (id: string, token: string) => request<void>(`/sender-connections/${id}`, token, { method: 'DELETE' }),
}

export const integrationsApi = {
  storeCredentials: (connectionId: string, payload: CredentialUpload, token: string) => request<CredentialStatus>(`/integrations/${connectionId}/credentials`, token, { method: 'POST', body: JSON.stringify(payload) }),
  listSenders: (connectionId: string, token: string) => request<SenderAccount[]>(`/integrations/${connectionId}/senders`, token),
  importSenders: (payload: SenderImportRequest, token: string) => request<SenderImportResponse>('/integrations/senders/import', token, { method: 'POST', body: JSON.stringify(payload) }),
  disableSender: (senderId: string, token: string) => request<SenderAccount>(`/integrations/senders/${senderId}/disable`, token, { method: 'POST' }),
  enableSender: (senderId: string, token: string) => request<SenderAccount>(`/integrations/senders/${senderId}/enable`, token, { method: 'POST' }),
  sendTestEmail: (senderId: string, payload: TestSendRequest, token: string) => request<TestSendResponse>(`/senders/${senderId}/test`, token, { method: 'POST', body: JSON.stringify(payload) }),
}

export const googleOAuthApi = {
  authorize: (payload: GoogleAuthorizeRequest, token: string) => request<GoogleAuthorizeResponse>('/senders/google/authorize', token, { method: 'POST', body: JSON.stringify(payload) }),
}

export const microsoftOAuthApi = {
  authorize: (payload: MicrosoftAuthorizeRequest, token: string) => request<MicrosoftAuthorizeResponse>('/senders/microsoft/authorize', token, { method: 'POST', body: JSON.stringify(payload) }),
  details: (connectionId: string, token: string) => request<MicrosoftConnectionDetails>(`/senders/microsoft/connections/${connectionId}`, token),
  validate: (connectionId: string, token: string) => request<MicrosoftConnectionDetails>(`/senders/microsoft/connections/${connectionId}/validate`, token, { method: 'POST' }),
  disconnect: (connectionId: string, token: string) => request<MicrosoftConnectionDetails>(`/senders/microsoft/connections/${connectionId}/disconnect`, token, { method: 'POST' }),
  reconnect: (connectionId: string, token: string) => request<MicrosoftAuthorizeResponse>(`/senders/microsoft/connections/${connectionId}/reconnect`, token, { method: 'POST' }),
  testSend: (connectionId: string, payload: MicrosoftTestSendRequest, token: string) => request<TestSendResponse>(`/senders/microsoft/connections/${connectionId}/test-send`, token, { method: 'POST', body: JSON.stringify(payload) }),
}