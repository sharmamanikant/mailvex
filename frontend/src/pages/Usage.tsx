import { useQuery } from '@tanstack/react-query'
import { Activity, Gauge, Sparkles, Zap } from 'lucide-react'
import { usageApi } from '../api/billing'
import type { UsageMetric } from '../types/billing'

export default function Usage({ accessToken }: { accessToken: string }) {
  const overview = useQuery({ queryKey: ['usage-overview'], queryFn: () => usageApi.overview(accessToken) })
  const plans = useQuery({ queryKey: ['usage-plans'], queryFn: () => usageApi.plans(accessToken) })
  const events = useQuery({ queryKey: ['usage-events'], queryFn: () => usageApi.events(accessToken, 25) })

  if (overview.isLoading) return <main className="usage-page"><div className="table-state">Loading usage...</div></main>

  if (overview.isError || !overview.data) {
    return <main className="usage-page"><div className="table-state error-state"><strong>Usage unavailable</strong><span>{overview.error instanceof Error ? overview.error.message : 'Loading failed'}</span></div></main>
  }

  const data = overview.data

  return <main className="usage-page">
    <div className="usage-heading">
      <div><p className="eyebrow">USAGE &amp; BILLING</p><h1>Usage &amp; plan</h1><p className="muted">Your workspace plan, current limits, and the activity that counts against this period.</p></div>
      <span className={`status-pill ${data.status === 'ACTIVE' ? '' : 'status-warning'}`}><span />{data.status}</span>
    </div>

    <section className="usage-plan">
      <div className="usage-plan-main"><span className="usage-plan-icon"><Sparkles size={18} /></span><div><p className="eyebrow">CURRENT PLAN</p><h2>{data.plan_name}</h2><p className="muted">${data.price_usd_mo}/month{data.price_usd_mo === 0 ? ' · free forever' : ''}</p></div></div>
      <div className="usage-plan-period"><span>Billing period</span><strong>{formatDate(data.period_start)} — {formatDate(data.period_end)}</strong></div>
      {data.features.length > 0 && <div className="usage-plan-features">{data.features.map((feature) => <span className="usage-feature" key={feature}><Zap size={12} />{feature}</span>)}</div>}
    </section>

    <section className="usage-metrics">
      {data.metrics.map((metric) => <UsageMeter metric={metric} key={metric.metric} />)}
    </section>

    <section className="report-panel">
      <div className="report-panel-heading"><div><h2>Available plans</h2><p className="muted">Capacity and pricing live in the plan catalog — no hard-coded limits.</p></div></div>
      {plans.isLoading ? <div className="table-state">Loading plans...</div> : plans.isError ? <div className="table-state error-state">{plans.error.message}</div> : <div className="plan-list">{plans.data?.map((plan) => <article className="plan-card" key={plan.code}><div className="plan-card-head"><strong>{plan.name}</strong><span>${plan.price_usd_mo}/mo</span></div><ul className="plan-card-limits">{plan.limits.filter((item) => item.limit !== null).map((item) => <li key={item.metric}><span>{item.label}</span><em>{formatLimit(item.metric, item.limit)}</em></li>)}</ul>{plan.features.length > 0 && <ul className="plan-card-features">{plan.features.map((feature) => <li key={feature}>{feature}</li>)}</ul>}</article>)}</div>}
    </section>

    <section className="report-panel">
      <div className="report-panel-heading"><div><h2>Recent activity</h2><p className="muted">Immutable usage events that meter this billing period.</p></div><span className="usage-activity-note"><Activity size={14} /> latest 25</span></div>
      {events.isLoading ? <div className="table-state">Loading events...</div> : events.isError ? <div className="table-state error-state">{events.error.message}</div> : events.data?.length ? <div className="report-table-wrap"><table className="report-table"><thead><tr><th>Event</th><th>Metric</th><th>Qty</th><th>Resource</th><th>Created</th></tr></thead><tbody>{events.data.map((row) => <tr key={row.id}><td><span className="report-status">{row.event_type}</span></td><td>{eventMetricLabel(row.event_type)}</td><td>{row.quantity}</td><td>{row.resource_type ?? '—'}</td><td>{row.created_at ? new Date(row.created_at).toLocaleString() : '—'}</td></tr>)}</tbody></table></div> : <div className="table-state"><Gauge size={25} /><strong>No usage activity yet</strong><span>Contact creation, AI generations, sends, and storage events appear here.</span></div>}
    </section>
  </main>
}

function UsageMeter({ metric }: { metric: UsageMetric }) {
  const percent = metric.limit === null ? null : Math.min(100, Math.round((metric.used / Math.max(1, metric.limit)) * 100))
  const near = percent !== null && percent >= 90
  return <article className={`usage-meter ${near ? 'usage-meter-near' : ''}`}>
    <div className="usage-meter-top"><span>{metric.label}</span><em>{metric.scope === 'period' ? 'per period' : 'current'}</em></div>
    <strong>{formatValue(metric.metric, metric.used)}<small> / {metric.limit === null ? 'unlimited' : formatValue(metric.metric, metric.limit)}</small></strong>
    {percent !== null && <div className="usage-bar"><div className={`usage-bar-fill ${near ? 'usage-bar-near' : ''}`} style={{ width: `${percent}%` }} /></div>}
    <small className="usage-remaining">{metric.remaining === null ? 'No limit applied' : `${metric.remaining.toLocaleString()} remaining`}</small>
  </article>
}

function formatDate(value: string): string {
  return new Date(value).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

function formatValue(metric: string, value: number): string {
  if (metric === 'storage_mb') return value >= 1024 ? `${(value / 1024).toFixed(1)} GB` : `${value} MB`
  return value.toLocaleString()
}

function formatLimit(metric: string, limit: number | null): string {
  if (limit === null) return 'Unlimited'
  return formatValue(metric, limit)
}

function eventMetricLabel(eventType: string): string {
  const eventToMetric: Record<string, string> = {
    CONTACT_CREATED: 'Contacts',
    AI_GENERATION: 'AI generations',
    CAMPAIGN_CREATED: 'Campaigns',
    MESSAGE_SENT: 'Messages sent',
    SENDER_CONNECTED: 'Connected senders',
    STORAGE_USED: 'Storage',
    TEAM_MEMBER_ADDED: 'Team members',
  }
  return eventToMetric[eventType] ?? eventType
}