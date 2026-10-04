export type User = {
  id: string
  tenant_id: string
  email: string
  display_name: string
  roles: string[]
}

export type TokenResponse = {
  access_token: string
  token_type: string
  expires_at: string
}
