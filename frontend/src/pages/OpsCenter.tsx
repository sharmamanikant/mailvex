import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, AlertTriangle, CheckCircle2, Gauge, RefreshCw, Server, ShieldCheck } from 'lucide-react'
import { useState } from 'react'
import { opsApi } from '../api/ops'
import type { MetricBucket, OpsAlert, OpsOverview } from '../types/ops'

export default function OpsCenter({ accessToken }: { accessToken: string }) {
  const queryClient = useQueryClient()
  const [evaluating, setEvaluating] = useState(false)
  const overview = useQuery({ queryKey: ['ops-overview'], queryFn: () => opsApi.overview(accessToken) })

  const evaluate = useMutation({
    mutationFn: () => opsApi.evaluate(accessToken),
    onMutate: () => setEvaluating(true),
    onSettled: () => setEvaluating(false),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['ops-overview'] }) },
  })
  const ack = useMutation({
    mutationFn: (alertId: string) => opsApi.ack(accessToken, alertId),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['ops-overview'] }) },
  })
  const runSample = useMutation({
    mutationFn: () => opsApi.sample(accessToken),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['ops-overview'] }) },
  })

  const refresh = () => { void queryClient.invalidateQueries({ queryKey: ['ops-overview'] }) }

  if (overview.isLoading) return <main className="usage-page"><div className="table-state">Loading operations...</div></main>

  if (overview.isError || !overview.data) {
    return <main className="usage-page"><div className="table-state error-state"><strong>Operations unavailable</strong><span>{overview.error instanceof Error ? overview.error.message : 'Loading failed'}</span></div></main>
  }

  const data = overview.data

  return <main className="usage-page ops-page">
    <div className="usage-heading">
      <div><p className="eyebrow">OPERATIONS</p><h1>Ops center</h1><p className="muted">System health, live metrics, and platform-wide alerts.</p></div>
      <span className={`status-pill ${data.ready ? '' : 'status-warning'}`}><span />{data.ready ? 'All systems operational' : 'System attention needed'}</span>
    </div>

    <section className="ops-actions">
      <button className="primary-button" onClick={() => evaluate.mutate()} disabled={evaluating}><RefreshCw size={15} />{evaluating ? 'Evaluating...' : 'Evaluate alerts'}</button>
      <button className="outline-button" onClick={() => runSample.mutate()} disabled={runSample.isPending}><Activity size={15} />Sample now</button>
      <button className="outline-button" onClick={refresh}><RefreshCw size={15} />Refresh</button>
    </section>

    <ReadinessPanel overview={data} />
    <MetricsPanel overview={data} />
    <AlertsPanel alerts={data.alerts.open} onAck={(id) => ack.mutate(id)} />
    <SamplesPanel overview={data} />
  </main>
}

function ReadinessPanel({ overview }: { overview: OpsOverview }) {
  const labels: Record<string, string> = { database: 'Database', redis: 'Redis', migrations: 'Migrations', worker: 'Worker', scheduler: 'Scheduler' }
  return <section className="report-panel">
    <div className="report-panel-heading"><div><h2>Readiness</h2><p className="muted">Dependency connectivity and process heartbeats.</p></div></div>
    <div className="ops-readiness">{Object.entries(overview.readiness).map(([key, state]) => (
      <article className={`ops-chip ${state === 'ready' ? 'ops-chip-ready' : 'ops-chip-failed'}`} key={key}>
        <span>{state === 'ready' ? <CheckCircle2 size={16} /> : <AlertTriangle size={16} />}</span>
        <div><strong>{labels[key] ?? key}</strong><small>{state === 'ready' ? 'Ready' : 'Failed'}</small></div>
      </article>
    ))}</div>
  </section>
}

function MetricsPanel({ overview }: { overview: OpsOverview }) {
  const entries = Object.entries(overview.metrics)
  return <section className="report-panel">
    <div className="report-panel-heading"><div><h2>Live metrics</h2><p className="muted">Counters, gauges, and latency buckets published by the API, workers, and scheduler.</p></div></div>
    {entries.length ? <div className="ops-metrics">{entries.map(([name, buckets]) => <article className="ops-metric" key={name}>
      <div className="ops-metric-head"><strong>{metricLabel(name)}</strong><code>{name}</code></div>
      <ul>{Object.entries(buckets).map(([label, bucket]) => <li key={label}><span>{label === '__default__' ? 'all' : labelKey(label)}</span><em>{formatBucket(bucket)}</em></li>)}</ul>
    </article>)}</div> : <div className="table-state"><Gauge size={25} /><strong>No metrics recorded yet</strong><span>Run a sample or send traffic to see counters.</span></div>}
  </section>
}

function AlertsPanel({ alerts, onAck }: { alerts: OpsAlert[]; onAck: (id: string) => void }) {
  return <section className="report-panel">
    <div className="report-panel-heading"><div><h2>Open alerts</h2><p className="muted">Active or acknowledged platform alerts awaiting resolution.</p></div>{alerts.length > 0 && <span className="usage-activity-note">{alerts.length} open</span>}</div>
    {alerts.length ? <div className="report-table-wrap"><table className="report-table"><thead><tr><th>Severity</th><th>Rule</th><th>Message</th><th>Created</th><th></th></tr></thead><tbody>{alerts.map((alert) => <tr key={alert.id}><td><span className={`ops-severity ${alert.severity === 'critical' ? 'ops-severity-critical' : 'ops-severity-warning'}`}>{alert.severity}</span></td><td>{alertRuleLabel(alert.rule)}</td><td>{alert.message}</td><td>{new Date(alert.created_at).toLocaleString()}</td><td>{alert.status === 'OPEN' ? <button className="outline-button" onClick={() => onAck(alert.id)}>Acknowledge</button> : <span className="report-status">{alert.status}</span>}</td></tr>)}</tbody></table></div> : <div className="table-state"><ShieldCheck size={25} /><strong>No open alerts</strong><span>Rules are checked on demand or by the scheduled evaluator.</span></div>}
  </section>
}

function SamplesPanel({ overview }: { overview: OpsOverview }) {
  const latest = Object.entries(overview.latest_samples)
  return <section className="report-panel">
    <div className="report-panel-heading"><div><h2>Latest samples</h2><p className="muted">Most recent persisted values per metric.</p></div><span className="usage-activity-note"><Server size={14} /> {new Date(overview.sampled_at).toLocaleString()}</span></div>
    {latest.length ? <div className="report-table-wrap"><table className="report-table"><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>{latest.map(([metric, value]) => <tr key={metric}><td>{metricLabel(metric)} <code>{metric}</code></td><td>{formatSampleValue(metric, value)}</td></tr>)}</tbody></table></div> : <div className="table-state"><Gauge size={25} /><strong>No samples yet</strong><span>Click "Sample now" to persist a point of system health.</span></div>}
  </section>
}

function metricLabel(metric: string): string {
  const labels: Record<string, string> = {
    'scheduler.dispatched': 'Scheduler dispatches',
    'jobs.scheduled': 'Messages scheduled',
    'jobs.scheduled.latency': 'Schedule latency',
    'queue.scheduled.due': 'Scheduled due queue',
    'queue.delivery.due': 'Delivery backlog',
    'jobs.delivery': 'Messages delivered',
    'jobs.delivery.retried': 'Delivery retries',
    'jobs.delivery.failures': 'Delivery failures',
    'jobs.delivery.latency': 'Delivery latency',
    'webhooks.received': 'Webhooks received',
    'webhooks.verification_failed': 'Webhook verification failures',
    'webhooks.latency': 'Webhook latency',
    'db.latency': 'Database latency',
    'redis.latency': 'Redis latency',
    'broker.depth': 'Broker depth',
    'worker.age': 'Worker heartbeat age',
    'scheduler.age': 'Scheduler heartbeat age',
  }
  return labels[metric] ?? metric
}

function alertRuleLabel(rule: string): string {
  const labels: Record<string, string> = {
    database_unavailable: 'Database unavailable',
    redis_unavailable: 'Redis unavailable',
    worker_stopped: 'Worker stopped',
    scheduler_stopped: 'Scheduler stopped',
    queue_growing: 'Queue growing',
    high_provider_failure_rate: 'High failure rate',
    webhook_verification_failure: 'Webhook verification',
    critical_sender_health: 'Critical sender health',
    disk_storage: 'Disk storage',
  }
  return labels[rule] ?? rule
}

function labelKey(label: string): string {
  return label.replace(/^k=/, '').replace(/&/g, ' · ')
}

function formatBucket(bucket: MetricBucket): string {
  const parts: string[] = []
  if (bucket.value !== undefined) parts.push(formatNumber(bucket.value))
  if (bucket.count !== undefined && bucket.count !== null) parts.push(`${formatNumber(bucket.count)} total`)
  if (bucket.avg !== undefined && bucket.avg !== null) parts.push(`avg ${formatNumber(bucket.avg)}`)
  if (bucket.max !== undefined && bucket.max !== null) parts.push(`max ${formatNumber(bucket.max)}`)
  return parts.join(' · ') || '0'
}

function formatNumber(value: number): string {
  if (Math.floor(value) === value && Math.abs(value) < 1e15) return value.toLocaleString()
  return value.toLocaleString(undefined, { maximumFractionDigits: 3 })
}

function formatSampleValue(metric: string, value: number | null): string {
  if (value === null) return '—'
  if (metric === 'db.latency' || metric === 'redis.latency') return `${value.toFixed(2)} ms`
  if (metric === 'worker.age' || metric === 'scheduler.age') return `${value.toFixed(1)}s`
  return formatNumber(value)
}