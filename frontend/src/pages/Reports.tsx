import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { AlertTriangle, Calendar, CircleDashed, Download, MailWarning, Megaphone, Send, ShieldAlert, ShieldCheck, UserX, Users } from 'lucide-react'
import { reportingApi } from '../api/reporting'
import type { ReportFilter, ReportName } from '../types/reporting'

const RANGE_OPTIONS = [
  { value: '', label: 'All time' },
  { value: 'today', label: 'Today' },
  { value: '7d', label: 'Last 7 days' },
  { value: '30d', label: 'Last 30 days' },
  { value: '90d', label: 'Last 90 days' },
  { value: 'custom', label: 'Custom range' },
]

function buildFilter(range: string, startDate: string, endDate: string): ReportFilter {
  if (range === 'custom') return { range: 'custom', start_date: startDate || undefined, end_date: endDate || undefined }
  if (!range) return {}
  return { range: range as ReportFilter['range'] }
}

export default function Reports({ accessToken }: { accessToken: string }) {
  const [range, setRange] = useState('')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const filter = buildFilter(range, startDate, endDate)
  const filterKey = JSON.stringify(filter)

  const dashboard = useQuery({ queryKey: ['reports-dashboard', filterKey], queryFn: () => reportingApi.dashboard(accessToken, filter) })
  const campaigns = useQuery({ queryKey: ['reports-campaigns', filterKey], queryFn: () => reportingApi.campaigns(accessToken, filter) })
  const senders = useQuery({ queryKey: ['reports-senders', filterKey], queryFn: () => reportingApi.senders(accessToken, filter) })
  const exportCsv = useMutation({ mutationFn: (report: ReportName) => reportingApi.exportCsv(report, accessToken, filter) })

  if (dashboard.isLoading) return <main className="reports-page"><div className="table-state">Loading business reports...</div></main>

  if (dashboard.isError || !dashboard.data) {
    return <main className="reports-page"><div className="table-state error-state"><strong>Reports unavailable</strong><span>{dashboard.error instanceof Error ? dashboard.error.message : 'Loading failed'}</span></div></main>
  }

  const { contacts, campaigns: campaignStats, delivery, senders: senderStats } = dashboard.data

  return <main className="reports-page">
    <div className="reports-heading">
      <div><p className="eyebrow">BUSINESS REPORTING</p><h1>Reports &amp; dashboard</h1><p className="muted">Tenant-scoped delivery, campaign, sender, and contact metrics for the selected window.</p></div>
      <div className="report-filters">
        <div className="report-filter"><Calendar size={15} /><select value={range} onChange={(event) => setRange(event.target.value)} aria-label="Report window"><option value="">All time</option>{RANGE_OPTIONS.slice(1).map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></div>
        {range === 'custom' && <>
          <input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} aria-label="Start date" />
          <input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} aria-label="End date" />
        </>}
      </div>
    </div>

    <section className="metric-grid">
      <article className="metric-card tone-green"><div className="metric-top"><span>Contacts</span><Users size={16} /></div><strong>{contacts.total}</strong><small>{contacts.active} active · {contacts.unsubscribed} unsubscribed · {contacts.bounced} bounced</small></article>
      <article className="metric-card tone-blue"><div className="metric-top"><span>Campaigns</span><Megaphone size={16} /></div><strong>{campaignStats.total}</strong><small>{campaignStats.active} active · {campaignStats.scheduled} scheduled · {campaignStats.completed} completed</small></article>
      <article className="metric-card tone-green"><div className="metric-top"><span>Sent</span><Send size={16} /></div><strong>{delivery.sent}</strong><small>{delivery.delivered} delivered · {delivery.bounced} bounced</small></article>
      <article className="metric-card tone-amber"><div className="metric-top"><span>Health flags</span><AlertTriangle size={16} /></div><strong>{senderStats.needs_attention}</strong><small>{delivery.blocked} blocked · {delivery.failed} failed · {delivery.complaints} complaints</small></article>
    </section>

    <section className="report-panel">
      <div className="report-panel-heading"><div><h2>Delivery summary</h2><p className="muted">Send-path funnel from the delivery queue plus de-duplicated provider events.</p></div></div>
      <div className="report-kpis">
        <ReportKpi label="Sent" value={delivery.sent} icon={Send} tone="green" />
        <ReportKpi label="Delivered" value={delivery.delivered} icon={CircleDashed} tone="green" />
        <ReportKpi label="Bounced" value={delivery.bounced} icon={MailWarning} tone="red" />
        <ReportKpi label="Complaints" value={delivery.complaints} icon={ShieldAlert} tone="red" />
        <ReportKpi label="Unsubscribed" value={delivery.unsubscribed} icon={UserX} tone="amber" />
        <ReportKpi label="Failed" value={delivery.failed} icon={AlertTriangle} tone="red" />
        <ReportKpi label="Blocked" value={delivery.blocked} icon={ShieldCheck} tone="amber" />
      </div>
    </section>

    <section className="report-panel">
      <div className="report-panel-heading"><div><h2>Campaign report</h2><p className="muted">Per-campaign recipients, sends, outcomes, and delivery rate.</p></div><button className="outline-button" onClick={() => exportCsv.mutate('campaigns')} disabled={exportCsv.isPending}><Download size={15} />{exportCsv.isPending ? 'Exporting...' : 'Export CSV'}</button></div>
      {campaigns.isLoading ? <div className="table-state">Loading campaigns...</div> : campaigns.isError ? <div className="table-state error-state">{campaigns.error.message}</div> : campaigns.data?.length ? <div className="report-table-wrap"><table className="report-table"><thead><tr><th>Campaign</th><th>Status</th><th>Recipients</th><th>Sent</th><th>Delivered</th><th>Delivery rate</th><th>Bounced</th><th>Blocked</th><th>Complaints</th><th>Unsubscribed</th></tr></thead><tbody>{campaigns.data.map((row) => <tr key={row.campaign_id}><td>{row.campaign_name}</td><td><span className="report-status">{row.status}</span></td><td>{row.recipients}</td><td>{row.sent}</td><td>{row.delivered}</td><td>{row.delivery_rate}%</td><td>{row.bounced}</td><td>{row.blocked}</td><td>{row.complaints}</td><td>{row.unsubscribed}</td></tr>)}</tbody></table></div> : <div className="table-state"><Megaphone size={25} /><strong>No campaign activity</strong><span>Campaigns you run in this window appear here.</span></div>}
    </section>

    <section className="report-panel">
      <div className="report-panel-heading"><div><h2>Sender report</h2><p className="muted">Per-sender volume, success, throttling, and health state.</p></div><button className="outline-button" onClick={() => exportCsv.mutate('senders')} disabled={exportCsv.isPending}><Download size={15} />{exportCsv.isPending ? 'Exporting...' : 'Export CSV'}</button></div>
      {senders.isLoading ? <div className="table-state">Loading senders...</div> : senders.isError ? <div className="table-state error-state">{senders.error.message}</div> : senders.data?.length ? <div className="report-table-wrap"><table className="report-table"><thead><tr><th>Sender</th><th>Status</th><th>Health</th><th>Score</th><th>Messages</th><th>Successful</th><th>Failed</th><th>Temporary</th><th>Throttling</th><th>Blocked</th></tr></thead><tbody>{senders.data.map((row) => <tr key={row.sender_id}><td><span className="report-sender"><strong>{row.sender_name}</strong><small>{row.sender_email}</small></span></td><td><span className="report-status">{row.status}</span></td><td><span className={`report-health ${row.health === 'NEEDS_ATTENTION' ? 'report-health-bad' : ''}`}>{row.health === 'NEEDS_ATTENTION' ? 'Needs attention' : 'Healthy'}</span></td><td>{row.health_score ?? '—'}</td><td>{row.messages}</td><td>{row.successful}</td><td>{row.failed}</td><td>{row.temporary_failures}</td><td>{row.provider_throttling}</td><td>{row.blocked}</td></tr>)}</tbody></table></div> : <div className="table-state"><ShieldCheck size={25} /><strong>No sender activity</strong><span>Senders that sent messages in this window appear here.</span></div>}
    </section>

    <section className="report-panel">
      <div className="report-panel-heading"><div><h2>Contact breakdown</h2><p className="muted">Account-level contact health for the reporting window.</p></div><button className="outline-button" onClick={() => exportCsv.mutate('contacts')} disabled={exportCsv.isPending}><Download size={15} />{exportCsv.isPending ? 'Exporting...' : 'Export CSV'}</button></div>
      <div className="contact-breakdown">
        <ContactMetric label="Total" value={contacts.total} />
        <ContactMetric label="Active" value={contacts.active} />
        <ContactMetric label="Unsubscribed" value={contacts.unsubscribed} />
        <ContactMetric label="Bounced" value={contacts.bounced} />
        <ContactMetric label="Suppressed" value={contacts.suppressed} />
        <ContactMetric label="Invalid" value={contacts.invalid} />
        <ContactMetric label="Inactive" value={contacts.inactive} />
      </div>
    </section>
  </main>
}

function ReportKpi({ label, value, icon: Icon, tone }: { label: string; value: number; icon: typeof Send; tone: string }) {
  return <div className={`report-kpi tone-${tone}`}><Icon size={16} /><span>{label}</span><strong>{value}</strong></div>
}

function ContactMetric({ label, value }: { label: string; value: number }) {
  return <div className="contact-breakdown-metric"><span>{label}</span><strong>{value}</strong></div>
}