export type ReportFilter = {
  range?: 'today' | '7d' | '30d' | '90d' | 'custom'
  start_date?: string
  end_date?: string
}

export type ContactReport = {
  total: number
  active: number
  unsubscribed: number
  suppressed: number
  invalid: number
  inactive: number
  bounced: number
}

export type CampaignOverview = {
  total: number
  active: number
  scheduled: number
  completed: number
  by_status: Record<string, number>
}

export type DeliveryOverview = {
  sent: number
  delivered: number
  bounced: number
  unsubscribed: number
  complaints: number
  temporary_failures: number
  blocked: number
  failed: number
}

export type SenderOverview = {
  total: number
  connected: number
  healthy: number
  needs_attention: number
}

export type DashboardReport = {
  tenant_id: string
  window: { range: string; start: string | null; end: string | null }
  contacts: ContactReport
  campaigns: CampaignOverview
  delivery: DeliveryOverview
  senders: SenderOverview
}

export type CampaignReportRow = {
  campaign_id: string
  campaign_name: string
  status: string
  recipients: number
  sent: number
  delivered: number
  bounced: number
  blocked: number
  unsubscribed: number
  complaints: number
  failed: number
  temporary_failures: number
  delivery_rate: number
}

export type SenderReportRow = {
  sender_id: string
  sender_name: string
  sender_email: string
  status: string
  health_score: number | null
  health: string
  messages: number
  successful: number
  failed: number
  temporary_failures: number
  provider_throttling: number
  blocked: number
}

export type ReportName = 'campaigns' | 'senders' | 'contacts'