export type InboxThread = {
  id: string
  sender_id: string
  sender_email: string | null
  external_thread_id: string
  subject: string | null
  last_message_at: string | null
  status: string
  match_status: string
  provider: string
  campaign_id: string | null
  contact_id: string | null
  contact_name: string | null
  snippet: string | null
}

export type InboxMessage = {
  id: string
  external_message_id: string
  direction: 'INBOUND' | 'OUTBOUND'
  from_email: string
  to_email: string
  subject: string
  body_text: string | null
  received_at: string | null
  status: string
}

export type InboxRecipient = {
  contact_id: string
  name: string
  email: string
  company: string | null
  designation: string | null
  campaign_name: string | null
  campaign_id: string | null
  last_contact: string | null
}

export type InboxThreadDetail = InboxThread & {
  messages: InboxMessage[]
  recipient: InboxRecipient | null
  campaign_name: string | null
}

export type InboxThreadPage = {
  items: InboxThread[]
  total: number
  page: number
  page_size: number
}

export type InboxSyncResult = {
  new_threads: number
  new_messages: number
}

export type InboxDraftResult = {
  draft_id: string
  thread_id: string
  subject: string
  body: string
  status: string
  provider: string
}

export type InboxReplyResult = {
  message_id: string
  thread_id: string
  direction: string
  status: string
}

export type AssistantAnalysis = {
  thread_id: string
  intent: string
  confidence: number
  unsubscribe: boolean
  suppressed: boolean
  requires_confirmation: boolean
  warnings: string[]
}

export type AssistantDraftResult = {
  id: string
  thread_id: string
  subject: string
  body: string
  status: string
  operation: string
  intent: string | null
  intent_confidence: number | null
  summary: string | null
  next_action: string | null
  warnings: string[]
  provider: string
  source_draft_id: string | null
}
