export type ReadinessKey = 'database' | 'redis' | 'migrations' | 'worker' | 'scheduler'
export type ReadinessState = 'ready' | 'failed'

export type Readiness = Record<ReadinessKey, ReadinessState>

export interface MetricBucket {
  value?: number
  count?: number | null
  sum?: number | null
  avg?: number | null
  max?: number | null
}

export type MetricSnapshot = Record<string, Record<string, MetricBucket>>

export interface OpsSample {
  id: string
  metric: string
  value: number | null
  labels: Record<string, string>;
  tenant_id: string | null
  sampled_at: string
}

export type AlertSeverity = 'critical' | 'warning'
export type AlertStatus = 'OPEN' | 'ACKNOWLEDGED' | 'RESOLVED'

export interface OpsAlert {
  id: string
  rule: string
  severity: AlertSeverity
  metric: string | null
  status: AlertStatus
  message: string
  details: Record<string, unknown> | null
  tenant_id: string | null
  created_at: string
  acknowledged_at: string | null
  resolved_at: string | null
}

export interface AlertRuleResult {
  rule: string
  label: string
  severity: AlertSeverity
  message: string
  details: Record<string, unknown> | null
}

export interface EvaluateResponse {
  results: AlertRuleResult[]
  created?: number
  updated?: number
  resolved?: number
}

export interface OpsOverview {
  readiness: Readiness
  ready: boolean
  metrics: Record<string, MetricSnapshot>
  samples: OpsSample[]
  latest_samples: Record<string, number | null>
  alerts: { open: OpsAlert[]; count: number }
  sampled_at: string
}