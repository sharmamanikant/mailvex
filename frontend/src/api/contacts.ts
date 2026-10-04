import type { BulkAction, BulkActionResult, BulkInput, BulkVerifyInput, Contact, ContactFieldDefinition, ContactFieldDefinitionInput, ContactFieldDefinitionUpdate, ContactInput, ContactList, ContactPage, ContactSegment, ContactSegmentInput, ContactTag, VerificationJob, VerificationSummary } from '../types/contacts'

const root = '/api/v1'
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${root}${path}`, { credentials: 'include', ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) } })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null
    const detail = payload?.detail
    throw new Error(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : 'Contact request failed')
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const contactsApi = {
  duplicates: (token: string) => request<Contact[][]>('/contacts/duplicates', { headers: { Authorization: `Bearer ${token}` } }),
  merge: (sourceId: string, targetId: string, token: string) => request<Contact>(`/contacts/duplicates/merge?source_id=${sourceId}&target_id=${targetId}`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } }),
  list: (params: URLSearchParams, token: string) => request<ContactPage>(`/contacts?${params}`, { headers: { Authorization: `Bearer ${token}` } }),
  get: (id: string, token: string) => request<Contact>(`/contacts/${id}`, { headers: { Authorization: `Bearer ${token}` } }),
  create: (input: ContactInput, token: string) => request<Contact>('/contacts', { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(input) }),
  update: (id: string, input: Partial<ContactInput>, token: string) => request<Contact>(`/contacts/${id}`, { method: 'PATCH', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(input) }),
  remove: (id: string, token: string) => request<void>(`/contacts/${id}`, { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } }),
  bulk: (input: BulkInput, token: string) => request<BulkActionResult>('/contacts/bulk', { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(input) }),
  lists: (token: string) => request<ContactList[]>('/contacts/lists', { headers: { Authorization: `Bearer ${token}` } }),
  createList: (input: { name: string; description?: string }, token: string) => request<ContactList>('/contacts/lists', { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(input) }),
  deleteList: (id: string, token: string) => request<void>(`/contacts/lists/${id}`, { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } }),
  tags: (token: string) => request<ContactTag[]>('/contacts/tags', { headers: { Authorization: `Bearer ${token}` } }),
  createTag: (input: { name: string }, token: string) => request<ContactTag>('/contacts/tags', { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(input) }),

  verifyContact: (id: string, probeSmtp: boolean, token: string) => request<{ contact_id: string; job_id: string; status: string; probe_smtp: boolean }>(`/contacts/${id}/verify`, { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify({ probe_smtp: probeSmtp }) }),
  bulkVerify: (input: BulkVerifyInput, token: string) => request<VerificationJob>('/contacts/bulk-verify', { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(input) }),
  verificationJob: (jobId: string, token: string) => request<VerificationJob>(`/contacts/verification-jobs/${jobId}`, { headers: { Authorization: `Bearer ${token}` } }),
  cancelVerificationJob: (jobId: string, token: string) => request<VerificationJob>(`/contacts/verification-jobs/${jobId}/cancel`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } }),
  verificationSummary: (token: string) => request<VerificationSummary>('/contacts/verification-summary', { headers: { Authorization: `Bearer ${token}` } }),

  fieldDefinitions: (token: string) => request<ContactFieldDefinition[]>('/contact-fields', { headers: { Authorization: `Bearer ${token}` } }),
  createFieldDefinition: (input: ContactFieldDefinitionInput, token: string) => request<ContactFieldDefinition>('/contact-fields', { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(input) }),
  updateFieldDefinition: (id: string, input: ContactFieldDefinitionUpdate, token: string) => request<ContactFieldDefinition>(`/contact-fields/${id}`, { method: 'PATCH', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(input) }),
  deleteFieldDefinition: (id: string, token: string) => request<void>(`/contact-fields/${id}`, { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } }),

  segments: (token: string) => request<ContactSegment[]>('/segments', { headers: { Authorization: `Bearer ${token}` } }),
  getSegment: (id: string, token: string) => request<ContactSegment>(`/segments/${id}`, { headers: { Authorization: `Bearer ${token}` } }),
  createSegment: (input: ContactSegmentInput, token: string) => request<ContactSegment>('/segments', { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(input) }),
  updateSegment: (id: string, input: ContactSegmentInput, token: string) => request<ContactSegment>(`/segments/${id}`, { method: 'PATCH', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(input) }),
  deleteSegment: (id: string, token: string) => request<void>(`/segments/${id}`, { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } }),
  segmentContacts: (id: string, params: URLSearchParams, token: string) => request<ContactPage>(`/segments/${id}/contacts?${params}`, { headers: { Authorization: `Bearer ${token}` } }),

  canonicalLists: (token: string) => request<ContactList[]>('/contact-lists', { headers: { Authorization: `Bearer ${token}` } }),
  createCanonicalList: (input: { name: string; description?: string }, token: string) => request<ContactList>('/contact-lists', { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(input) }),
  getCanonicalList: (id: string, token: string) => request<ContactList>(`/contact-lists/${id}`, { headers: { Authorization: `Bearer ${token}` } }),
  updateCanonicalList: (id: string, input: { name?: string; description?: string }, token: string) => request<ContactList>(`/contact-lists/${id}`, { method: 'PATCH', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(input) }),
  deleteCanonicalList: (id: string, token: string) => request<void>(`/contact-lists/${id}`, { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } }),
  listContacts: (id: string, params: URLSearchParams, token: string) => request<ContactPage>(`/contact-lists/${id}/contacts?${params}`, { headers: { Authorization: `Bearer ${token}` } }),
  addListMembers: (id: string, contactIds: string[], token: string) => request<{ added: number }>(`/contact-lists/${id}/members`, { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify({ contact_ids: contactIds }) }),
  removeListMember: (listId: string, contactId: string, token: string) => request<void>(`/contact-lists/${listId}/members/${contactId}`, { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } }),

  canonicalTags: (token: string) => request<ContactTag[]>('/tags', { headers: { Authorization: `Bearer ${token}` } }),
  createCanonicalTag: (input: { name: string }, token: string) => request<ContactTag>('/tags', { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(input) }),
  updateCanonicalTag: (id: string, input: { name: string }, token: string) => request<ContactTag>(`/tags/${id}`, { method: 'PATCH', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(input) }),
  deleteCanonicalTag: (id: string, token: string) => request<void>(`/tags/${id}`, { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } }),
}

export type { BulkAction, BulkInput }