export type MailboxUserType = 'USER' | 'ALIAS' | 'GROUP' | 'OTHER'

export type MailboxStatus = 'ACTIVE' | 'SUSPENDED' | 'DELETED' | 'UNKNOWN'

export interface WorkspaceMailbox {
  id: string
  provider_connection_id: string
  provider_mailbox_id: string
  email: string
  display_name: string | null
  first_name: string | null
  last_name: string | null
  department: string | null
  job_title: string | null
  user_type: MailboxUserType
  provider_status: MailboxStatus
  is_suspended: boolean
  is_deleted: boolean
  last_discovered_at: string | null
  created_at: string
  updated_at: string
}

export interface MailboxPage {
  items: WorkspaceMailbox[]
  page: number
  page_size: number
  total: number
}