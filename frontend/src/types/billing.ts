export type UsageScope = 'absolute' | 'period'

export type UsageMetric = {
  metric: string
  label: string
  used: number
  limit: number | null
  remaining: number | null
  scope: UsageScope
}

export type UsageOverview = {
  tenant_id: string
  plan_code: string
  plan_name: string
  price_usd_mo: number
  features: string[]
  status: string
  period_start: string
  period_end: string
  metrics: UsageMetric[]
}

export type PlanLimitRow = {
  metric: string
  label: string
  limit: number | null
  scope: UsageScope
}

export type BillingPlan = {
  code: string
  name: string
  price_usd_mo: number
  features: string[]
  limits: PlanLimitRow[]
}

export type UsageEventRow = {
  id: string
  event_type: string
  period_start: string | null
  quantity: number
  resource_type: string | null
  resource_id: string | null
  metadata: Record<string, unknown>
  created_at: string | null
}