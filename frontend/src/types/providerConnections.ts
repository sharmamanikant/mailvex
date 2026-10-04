export interface ProviderConnection {
  id: string
  provider: 'GOOGLE' | 'MICROSOFT' | 'SENDGRID' | 'ZOHO' | 'SMTP'
  connection_type: 'OAUTH' | 'API_KEY' | 'SMTP'
  provider_account_id: string | null
  workspace_domain: string | null
  display_name: string | null
  status: 'CONNECTING' | 'CONNECTED' | 'ERROR' | 'DISCONNECTED' | 'REVOKED'
  scopes: string[]
  credential_configured: boolean
  credential_expires_at: string | null
  connected_by: string | null
  last_sync_at: string | null
  last_sync_status: 'IDLE' | 'SYNCING' | 'COMPLETED' | 'FAILED' | null
  last_sync_error: string | null
  last_sync_started_at: string | null
  last_sync_completed_at: string | null
  provider_metadata: {
    organizationName: string | null
    defaultDomain: string | null
    microsoftTenantId: string | null
  }
  last_sync_stats: {
    created?: number
    updated?: number
    suspended?: number
    deleted?: number
    skipped?: number
    errors?: number
    duration_ms?: number
    completed_at?: string
  }
  created_at: string
  updated_at: string
}

export interface GoogleConnectResponse {
  authorization_url: string
}
