export type Sender = {
  id: string
  tenant_id: string
  email: string
  display_name: string | null
  reply_to: string | null
  provider: 'GOOGLE' | 'MICROSOFT' | 'SMTP' | 'FUTURE_ESP'
  status: 'CONNECTED' | 'REAUTH_REQUIRED' | 'DISABLED' | 'DISCONNECTED' | 'SUSPENDED' | 'HEALTH_WARNING' | 'HEALTH_CRITICAL'
  connection_status: 'CONNECTED' | 'REAUTH_REQUIRED' | 'DISCONNECTED' | 'SUSPENDED'
  health_score: number | null
  timezone: string
  smtp_host: string | null
  smtp_port: number | null
  smtp_tls_mode: 'TLS' | 'STARTTLS' | 'SSL' | null
  smtp_username: string | null
  company: string | null
  designation: string | null
  phone: string | null
  signature: string | null
  last_connected_at: string | null
  last_used_at: string | null
  created_at: string
  updated_at: string
}

export type SenderInput = {
  email: string
  display_name?: string
  reply_to?: string
  provider: Sender['provider']
  timezone: string
  company?: string
  designation?: string
  phone?: string
  signature?: string
  encrypted_credential_ref?: string
  encryption_key_version?: string
  smtp_host?: string
  smtp_port?: number
  smtp_tls_mode?: 'TLS' | 'STARTTLS' | 'SSL'
  smtp_username?: string
  smtp_password?: string
}

export type ReconnectResult = {
  sender_id: string
  provider: string
  authorization_url: string
}

export type SenderSelectionMode = 'EXPLICIT' | 'ALL_ELIGIBLE'

// ------------------------------------------------------------------ //
// Workspace Sender (Phase 3 + 4) — the application-level sending identity
// created from an eligible workspace mailbox. Distinct from the legacy
// ``Sender`` (EmailAccount-based) above.
// ------------------------------------------------------------------ //
export type WorkspaceSenderStatus = 'ACTIVE' | 'DISABLED' | 'ERROR' | 'REVOKED' | 'REMOVED'
export type WorkspaceSenderHealthStatus = 'UNKNOWN' | 'HEALTHY' | 'WARNING' | 'CRITICAL' | 'CHECKING'

export type WorkspaceSenderAvailability = {
  available: boolean
  sending_enabled: boolean
  sender_status: WorkspaceSenderStatus
  mailbox_status: string
  provider_connection_status: string
  reason:
    | 'SENDER_DISABLED'
    | 'SENDER_REMOVED'
    | 'SENDER_ERROR'
    | 'SENDER_REVOKED'
    | 'PROVIDER_DISCONNECTED'
    | 'PROVIDER_REVOKED'
    | 'MAILBOX_SUSPENDED'
    | 'MAILBOX_DELETED'
    | 'MAILBOX_UNAVAILABLE'
    | null
}

export type WorkspaceSender = {
  id: string
  tenant_id: string
  mailbox_id: string
  provider_connection_id: string
  email: string
  display_name: string | null
  provider: string
  status: WorkspaceSenderStatus
  sending_enabled: boolean
  health_status: WorkspaceSenderHealthStatus
  health_score: number | null
  last_health_check_at: string | null
  availability: WorkspaceSenderAvailability
  created_at: string
  updated_at: string
}

export type WorkspaceSenderMailbox = {
  id: string
  email: string
  display_name: string | null
  department: string | null
  job_title: string | null
  status: string
  is_suspended: boolean
  is_deleted: boolean
  last_discovered_at: string | null
}

export type WorkspaceSenderProviderConnection = {
  id: string
  provider: string
  status: string
  workspace_domain: string | null
  connection_type: string | null
  last_sync_status: string | null
  last_sync_completed_at: string | null
}

export type WorkspaceSenderHealth = {
  status: WorkspaceSenderHealthStatus
  score: number | null
  last_checked_at: string | null
}

export type WorkspaceSenderDetail = {
  id: string
  tenant_id: string
  mailbox_id: string
  provider_connection_id: string
  email: string
  display_name: string | null
  provider: string
  status: WorkspaceSenderStatus
  sending_enabled: boolean
  availability: WorkspaceSenderAvailability
  mailbox: WorkspaceSenderMailbox | null
  provider_connection: WorkspaceSenderProviderConnection | null
  health: WorkspaceSenderHealth
  created_at: string
  updated_at: string
}

export type SenderPage = {
  items: WorkspaceSender[]
  page: number
  page_size: number
  total: number
  total_pages: number
}

// ------------------------------------------------------------------ //
// Phase 5 Sender health engine API. Scores are Decimals on the wire and
// are normalized to numbers by the API client layer.
// ------------------------------------------------------------------ //
export type SenderCheckStatus = 'PASS' | 'WARNING' | 'FAIL' | 'UNKNOWN' | 'NOT_APPLICABLE'
export type SenderSeverity = 'INFO' | 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'
export type SenderHealthTrigger = 'MANUAL' | 'SCHEDULED' | 'SYSTEM'
export type SenderOverallHealthStatus = 'UNKNOWN' | 'HEALTHY' | 'WARNING' | 'CRITICAL'

export type SenderHealthCheckResult = {
  id: string | null
  check_type: string
  status: SenderCheckStatus
  score: number | null
  severity: SenderSeverity
  title: string
  summary: string | null
  technical_details: string | null
  recommendation: string | null
  metadata: Record<string, unknown>
  checked_at: string
}

export type SenderScoreExplanation = {
  version: string
  weights: Record<string, number>
  unknown_handling: string
  thresholds: Record<string, number>
}

export type SenderHealthCheck = {
  health_check_id: string
  sender_id: string
  tenant_id: string
  overall_status: SenderOverallHealthStatus
  overall_score: number | null
  score_version: string
  triggered_by: SenderHealthTrigger
  started_at: string
  completed_at: string | null
  duration_ms: number | null
  error_code: string | null
  error_message: string | null
  results: SenderHealthCheckResult[]
  score_explanation: SenderScoreExplanation | null
}

export type SenderHealthOverview = {
  id: string
  email: string
  provider: string
  health_status: SenderOverallHealthStatus
  health_score: number | null
  last_health_check_at: string | null
  latest: SenderHealthCheck | null
  summary: string | null
  domain_authentication_summary: SenderHealthCheckResult[]
}

export type SenderHealthHistoryItem = {
  health_check_id: string
  triggered_by: SenderHealthTrigger
  overall_status: SenderOverallHealthStatus
  overall_score: number | null
  score_version: string
  started_at: string
  completed_at: string | null
  duration_ms: number | null
  error_code: string | null
  result_count: number
}

export type SenderHealthHistory = {
  sender_id: string
  items: SenderHealthHistoryItem[]
  page: number
  page_size: number
  total: number
  total_pages: number
}

export type SenderPatchInput = {
  sending_enabled?: boolean
  display_name?: string
}

export type SenderOperation = {
  sender_id: string
  status: WorkspaceSenderStatus
  message: string
}

export type SenderBulkCreateRequest = {
  selection_mode: SenderSelectionMode
  mailbox_ids: string[]
}

export type SenderBulkCreateResponse = {
  mode: 'SYNC' | 'ASYNC'
  created: number
  already_exists: number
  skipped: number
  failed: number
  total_attempted: number
  senders: Sender[]
  request_id: string | null
  status: string | null
  selection_mode: SenderSelectionMode
}
