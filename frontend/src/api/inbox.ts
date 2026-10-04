import type { AssistantAnalysis, AssistantDraftResult, InboxDraftResult, InboxReplyResult, InboxSyncResult, InboxThreadDetail, InboxThreadPage } from '../types/inbox'

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials: 'include', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) }, ...init })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(payload?.detail ?? 'Inbox request failed')
  }
  return response.json() as Promise<T>
}

export const inboxApi = {
  listThreads: (token: string, params: { status?: string; senderId?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.status) query.set('status', params.status)
    if (params.senderId) query.set('sender_id', params.senderId)
    const qs = query.toString()
    return request<InboxThreadPage>(`/inbox/threads${qs ? `?${qs}` : ''}`, token)
  },
  getThread: (id: string, token: string) => request<InboxThreadDetail>(`/inbox/threads/${id}`, token),
  setStatus: (id: string, status: string, token: string) => request<InboxThreadDetail>(`/inbox/threads/${id}/status`, token, { method: 'POST', body: JSON.stringify({ status }) }),
  sync: (senderId: string, token: string) => request<InboxSyncResult>('/inbox/sync', token, { method: 'POST', body: JSON.stringify({ sender_id: senderId }) }),
  draft: (threadId: string, token: string) => request<InboxDraftResult>(`/inbox/threads/${threadId}/reply/draft`, token, { method: 'POST', body: JSON.stringify({}) }),
  approve: (threadId: string, draftId: string, token: string) => request<{ draft_id: string; thread_id: string; status: string }>(`/inbox/threads/${threadId}/reply/approve`, token, { method: 'POST', body: JSON.stringify({ draft_id: draftId }) }),
  send: (threadId: string, subject: string, body: string, draftId: string | null, token: string) => request<InboxReplyResult>(`/inbox/threads/${threadId}/reply`, token, { method: 'POST', body: JSON.stringify({ subject, body, draft_id: draftId }) }),
  analyze: (threadId: string, token: string) => request<AssistantAnalysis>(`/inbox/threads/${threadId}/assistant/analyze`, token, { method: 'POST', body: JSON.stringify({}) }),
  assistantDraft: (threadId: string, token: string, confirmUnsubscribe = false) => request<AssistantDraftResult>(`/inbox/threads/${threadId}/assistant/draft`, token, { method: 'POST', body: JSON.stringify({ confirm_unsubscribe: confirmUnsubscribe }) }),
  summarize: (threadId: string, token: string) => request<AssistantDraftResult>(`/inbox/threads/${threadId}/assistant/summarize`, token, { method: 'POST', body: JSON.stringify({}) }),
  nextAction: (threadId: string, token: string) => request<AssistantDraftResult>(`/inbox/threads/${threadId}/assistant/next-action`, token, { method: 'POST', body: JSON.stringify({}) }),
  transform: (draftId: string, operation: string, token: string, opts: { tone?: string | null; language?: string | null } = {}) => request<AssistantDraftResult>(`/inbox/drafts/${draftId}/transform`, token, { method: 'POST', body: JSON.stringify({ operation, tone: opts.tone ?? null, language: opts.language ?? null }) }),
  rejectDraft: (draftId: string, token: string) => request<AssistantDraftResult>(`/inbox/drafts/${draftId}/reject`, token, { method: 'POST', body: JSON.stringify({}) }),
}
