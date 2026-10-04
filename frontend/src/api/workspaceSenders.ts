import type {
  SenderBulkCreateRequest,
  SenderBulkCreateResponse,
  SenderHealthCheck,
  SenderHealthHistory,
  SenderHealthOverview,
  SenderOperation,
  SenderPage,
  SenderPatchInput,
  WorkspaceSender,
  WorkspaceSenderDetail,
} from '../types/senders'

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      ...(init?.headers ?? {}),
    },
    ...init,
  })
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: unknown } | null
    const detail = payload?.detail
    const message = typeof detail === 'string' ? detail : (detail as { detail?: string } | null)?.detail
    throw new Error(message ?? 'Sender request failed')
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

// Score fields are Decimals on the wire (JSON strings like "92.0000000000");
// normalize them to numbers/null so the UI can render them directly.
function normalizeScore(value: number | string | null | undefined): number | null {
  if (value == null || value === '') return null
  const converted = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(converted) ? converted : null
}

const normalizeSender = (sender: WorkspaceSender): WorkspaceSender => ({
  ...sender,
  health_score: normalizeScore(sender.health_score),
})

const normalizeResult = (result: SenderHealthCheck['results'][number]): SenderHealthCheck['results'][number] => ({
  ...result,
  score: normalizeScore(result.score),
})

const normalizeHealthCheck = (check: SenderHealthCheck): SenderHealthCheck => ({
  ...check,
  overall_score: normalizeScore(check.overall_score),
  results: check.results.map(normalizeResult),
})

const normalizeOverview = (overview: SenderHealthOverview): SenderHealthOverview => ({
  ...overview,
  health_score: normalizeScore(overview.health_score),
  latest: overview.latest ? normalizeHealthCheck(overview.latest) : null,
  domain_authentication_summary: overview.domain_authentication_summary.map(normalizeResult),
})

const normalizeHistory = (history: SenderHealthHistory): SenderHealthHistory => ({
  ...history,
  items: history.items.map((item) => ({ ...item, overall_score: normalizeScore(item.overall_score) })),
})

export const workspaceSendersApi = {
  list: (params: URLSearchParams, token: string) =>
    request<SenderPage>(`/senders?${params}`, token).then((page) => ({
      ...page,
      items: page.items.map(normalizeSender),
    })),
  get: (id: string, token: string) => request<WorkspaceSender>(`/senders/${id}`, token).then(normalizeSender),
  getDetail: (id: string, token: string) =>
    request<WorkspaceSenderDetail>(`/senders/${id}`, token).then((detail) => ({
      ...detail,
      health: { ...detail.health, score: normalizeScore(detail.health.score) },
    })),
  update: (id: string, input: Partial<SenderPatchInput>, token: string) =>
    request<WorkspaceSender>(`/senders/${id}`, token, { method: 'PATCH', body: JSON.stringify(input) }).then(
      normalizeSender,
    ),
  enable: (id: string, token: string) => request<SenderOperation>(`/senders/${id}/enable`, token, { method: 'POST' }),
  disable: (id: string, token: string) => request<SenderOperation>(`/senders/${id}/disable`, token, { method: 'POST' }),
  restore: (id: string, token: string) => request<SenderOperation>(`/senders/${id}/restore`, token, { method: 'POST' }),
  remove: (id: string, token: string) => request<void>(`/senders/${id}`, token, { method: 'DELETE' }),
  bulkCreate: (connectionId: string, payload: SenderBulkCreateRequest, token: string) =>
    request<SenderBulkCreateResponse>(`/provider-connections/${connectionId}/mailboxes/senders`, token, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  runHealthCheck: (id: string, token: string) =>
    request<SenderHealthCheck>(`/senders/${id}/health-check`, token, { method: 'POST' }).then(normalizeHealthCheck),
  getHealth: (id: string, token: string) => request<SenderHealthOverview>(`/senders/${id}/health`, token).then(normalizeOverview),
  getHealthHistory: (id: string, params: URLSearchParams, token: string) =>
    request<SenderHealthHistory>(`/senders/${id}/health/history?${params}`, token).then(normalizeHistory),
}