export type ConnectionType = 'OAUTH' | 'API_KEY' | 'SMTP'

export type ProviderCapability = {
  provider_name: string
  display_name: string
  connection_types: string[]
  supports_oauth: boolean
  supports_api_key: boolean
  supports_smtp: boolean
  supports_sender_discovery: boolean
  supports_webhooks: boolean
  supports_inbox_sync: boolean
}

export type IntegrationCreate = {
  provider: string
  connection_type: ConnectionType
  external_account_id?: string | null
  email?: string | null
  smtp_host?: string | null
  smtp_port?: number | null
  smtp_username?: string | null
  metadata?: Record<string, unknown>
}

export type SenderConnection = {
  id: string
  tenant_id: string
  provider: string
  connection_type: string
  status: string
  external_account_id: string | null
  email: string | null
  credential_configured: boolean
  credential_version: string
  credential_expires_at: string | null
  metadata: Record<string, unknown>
  last_connected_at: string | null
  created_at: string
  updated_at: string
}

export type CredentialUpload = {
  api_key?: string | null
  smtp_password?: string | null
  client_secret?: string | null
  access_token?: string | null
  refresh_token?: string | null
  client_id?: string | null
  smtp_username?: string | null
  smtp_host?: string | null
  smtp_port?: number | null
  tenant_id?: string | null
  scopes?: string[]
  expires_at?: string | null
}

export type CredentialStatus = {
  connection_id: string
  configured: boolean
  credential_version: string
  expires_at: string | null
}

export type DiscoveredSender = {
  email: string
  display_name: string | null
  external_sender_id: string | null
  verified: boolean
}

export type DiscoveryResponse = {
  provider: string
  supports_discovery: boolean
  discovered: number
  senders: DiscoveredSender[]
  errors: string[]
}

export type SenderAccount = {
  id: string
  tenant_id: string
  connection_id: string
  email: string
  display_name: string | null
  provider: string
  external_sender_id: string | null
  status: string
  health_status: string
  last_used_at: string | null
  created_at: string
  updated_at: string
}

export type SenderImportRequest = {
  connection_id: string
  senders: Array<{ email: string; display_name?: string | null; external_sender_id?: string | null }>
}

export type SenderImportResponse = {
  imported: number
  skipped: number
  accounts: SenderAccount[]
  errors: string[]
}

export type GoogleAuthorizeRequest = {
  connection_id: string
}

export type GoogleAuthorizeResponse = {
  authorization_url: string
}

export type MicrosoftAuthorizeRequest = {
  connection_id: string
}

export type MicrosoftAuthorizeResponse = {
  authorization_url: string
}

export type MicrosoftConnectionDetails = {
  id: string
  tenant_id: string
  provider: string
  connection_type: string
  status: string
  email: string | null
  external_account_id: string | null
  credential_configured: boolean
  credential_version: string | null
  credential_expires_at: string | null
  metadata: Record<string, unknown>
  last_connected_at: string | null
  last_error_code: string | null
  last_error_at: string | null
}

export type MicrosoftTestSendRequest = {
  recipient: string
}

export type TestSendRequest = {
  recipient: string
}

export type TestSendResponse = {
  sender_id: string
  connection_id: string
  provider: string
  from_email: string
  recipient: string
  message_id: string
  sent_at: string
}