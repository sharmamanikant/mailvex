import { useState } from 'react'
import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ArrowUpRight, BarChart3, CalendarDays, CheckCircle2, ChevronRight, CircleAlert, FilePlus2, MailPlus, Megaphone, Plus, RefreshCw, Send, ShieldAlert, Sparkles, Users, UserPlus } from 'lucide-react'
import { Link } from 'react-router-dom'
import { campaignsApi } from '../api/campaigns'
import { reportingApi } from '../api/reporting'
import type { Campaign } from '../types/campaigns'
import type { ReportFilter } from '../types/reporting'
import type { User } from '../types/auth'

const RANGE_OPTIONS: { value: NonNullable<ReportFilter['range']>; label: string }[] = [
  { value: 'today', label: 'Today' },
  { value: '7d', label: 'Last 7 days' },
  { value: '30d', label: 'Last 30 days' },
  { value: '90d', label: 'Last 90 days' },
]

export default function AnalyticsDashboard({ accessToken, user }: { accessToken: string; user: User }) {
  const [range, setRange] = useState<ReportFilter['range']>('7d')
  const filter = { range }
  const filterKey = JSON.stringify(filter)
  const dashboard = useQuery({ queryKey: ['dashboard-report', filterKey], queryFn: () => reportingApi.dashboard(accessToken, filter) })
  const campaignReport = useQuery({ queryKey: ['dashboard-campaigns', filterKey], queryFn: () => reportingApi.campaigns(accessToken, filter) })
  const campaigns = useQuery({ queryKey: ['dashboard-campaign-list'], queryFn: () => campaignsApi.list(accessToken) })

  if (dashboard.isLoading) return <DashboardSkeleton />
  if (dashboard.isError || !dashboard.data) return <DashboardError onRetry={() => void dashboard.refetch()} />

  const report = dashboard.data
  const firstName = user.display_name.split(' ')[0]
  const campaignRows = campaignReport.data ?? []
  const attention = buildAttention(report, campaignRows)
  const scheduled = (campaigns.data ?? []).filter((campaign) => campaign.scheduled_at && new Date(campaign.scheduled_at).getTime() >= Date.now()).sort((left, right) => new Date(left.scheduled_at ?? 0).getTime() - new Date(right.scheduled_at ?? 0).getTime()).slice(0, 4)
  const deliveryRate = report.delivery.sent > 0 ? Math.round((report.delivery.delivered / report.delivery.sent) * 1000) / 10 : 0

  return <main className="production-dashboard">
    <section className="dashboard-welcome"><div><p className="eyebrow">WORKSPACE OVERVIEW</p><h1>Welcome back, {firstName}!</h1><p className="muted">Here&apos;s what&apos;s happening with your outreach today.</p></div><label className="dashboard-range"><CalendarDays size={16} /><span className="sr-only">Dashboard date range</span><select value={range} onChange={(event) => setRange(event.target.value as ReportFilter['range'])}>{RANGE_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label></section>
    <section className="dashboard-kpis"><Kpi label="Total Contacts" value={report.contacts.total} detail={`${report.contacts.active} active contacts`} icon={Users} tone="blue" /><Kpi label="Active Campaigns" value={report.campaigns.active} detail={`${report.campaigns.scheduled} scheduled`} icon={Megaphone} tone="violet" /><Kpi label="Emails Sent" value={report.delivery.sent} detail={`${report.delivery.delivered} delivered`} icon={Send} tone="green" /><Kpi label="Delivery Rate" value={`${deliveryRate}%`} detail={report.delivery.sent ? `${report.delivery.bounced} bounced` : 'No sends in this period'} icon={CheckCircle2} tone="amber" /></section>
    <section className="dashboard-primary-grid"><CampaignPerformance rows={campaignRows} loading={campaignReport.isLoading} /><AttentionPanel items={attention} /></section>
    <section className="dashboard-secondary-grid"><TopCampaigns rows={campaignRows} /><QuickActions /></section>
    <section className="dashboard-bottom-grid"><ActivityPanel /><SchedulePanel campaigns={scheduled} loading={campaigns.isLoading} error={campaigns.isError} /></section>
  </main>
}

function Kpi({ label, value, detail, icon: Icon, tone }: { label: string; value: number | string; detail: string; icon: typeof Users; tone: string }) { return <article className={`dashboard-kpi kpi-${tone}`}><span className="kpi-icon"><Icon size={18} /></span><div><span className="kpi-label">{label}</span><strong>{value}</strong><small>{detail}</small></div></article> }

function CampaignPerformance({ rows, loading }: { rows: { campaign_id: string; campaign_name: string; sent: number; delivered: number; bounced: number; delivery_rate: number }[]; loading: boolean }) {
  const max = Math.max(...rows.slice(0, 5).map((row) => row.sent), 1)
  return <section className="dashboard-card performance-card"><CardHeader title="Campaign Performance" subtitle="Delivery outcomes for the selected period" action={<Link to="/reports">View report <ArrowUpRight size={14} /></Link>} />{loading ? <SkeletonRows count={4} /> : rows.length ? <><div className="performance-legend"><span><i className="legend-sent" />Sent</span><span><i className="legend-delivered" />Delivered</span><span><i className="legend-bounced" />Bounced</span></div><div className="campaign-bars" role="img" aria-label="Campaign sent and delivered comparison">{rows.slice(0, 5).map((row) => <div className="campaign-bar-row" key={row.campaign_id}><span title={row.campaign_name}>{row.campaign_name}</span><div className="bar-track"><i className="bar-sent" style={{ width: `${Math.max((row.sent / max) * 100, row.sent ? 3 : 0)}%` }} /><i className="bar-delivered" style={{ width: `${Math.max((row.delivered / max) * 100, row.delivered ? 2 : 0)}%` }} /></div><strong>{row.delivery_rate}%</strong></div>)}</div></> : <EmptyState icon={BarChart3} title="No campaign activity" text="Campaign performance will appear here once a campaign sends." action="Create Campaign" href="/campaigns/new" />}</section>
}

function AttentionPanel({ items }: { items: AttentionItem[] }) { return <section className="dashboard-card attention-card"><CardHeader title="Needs Attention" badge={items.length || undefined} action={<Link to="/senders">View all <ArrowUpRight size={14} /></Link>} />{items.length ? <div className="attention-list">{items.slice(0, 4).map((item) => <Link className="attention-item" to={item.href} key={item.title}><span className={`attention-icon ${item.tone}`}><item.icon size={16} /></span><span><strong>{item.title}</strong><small>{item.description}</small></span><ChevronRight size={16} /></Link>)}</div> : <EmptyState icon={CheckCircle2} title="Everything looks good" text="No sender, contact, or campaign issues need your attention." />}</section> }

function TopCampaigns({ rows }: { rows: { campaign_id: string; campaign_name: string; sent: number; delivery_rate: number }[] }) { return <section className="dashboard-card"><CardHeader title="Top Campaigns" action={<Link to="/campaigns">View all <ArrowUpRight size={14} /></Link>} />{rows.length ? <div className="top-campaign-list">{rows.slice().sort((a, b) => b.sent - a.sent).slice(0, 5).map((row) => <Link className="top-campaign" to={`/campaigns/${row.campaign_id}`} key={row.campaign_id}><span className="top-campaign-copy"><strong>{row.campaign_name}</strong><small>{row.sent.toLocaleString()} sent</small></span><span className="progress"><i style={{ width: `${Math.min(Math.max(row.delivery_rate, 0), 100)}%` }} /></span><b>{row.delivery_rate}%</b></Link>)}</div> : <EmptyState icon={Megaphone} title="No campaigns yet" text="Create your first campaign." action="Create Campaign" href="/campaigns/new" />}</section> }

function QuickActions() { const actions = [{ label: 'Add Contact', href: '/contacts/new', icon: UserPlus }, { label: 'Import Contacts', href: '/contacts/import', icon: FilePlus2 }, { label: 'Create Campaign', href: '/campaigns/new', icon: Megaphone }, { label: 'AI Message Studio', href: '/ai/message-studio', icon: Sparkles }, { label: 'Connect Email', href: '/senders', icon: MailPlus }, { label: 'Create Template', href: '/templates/new', icon: Plus }]; return <section className="dashboard-card"><CardHeader title="Quick Actions" /><div className="quick-actions">{actions.map(({ label, href, icon: Icon }) => <Link className="quick-action" to={href} key={label}><Icon size={17} /><span>{label}</span><ChevronRight size={14} /></Link>)}</div></section> }

function ActivityPanel() { return <section className="dashboard-card"><CardHeader title="Recent Activity" action={<Link to="/reports">View all <ArrowUpRight size={14} /></Link>} /><EmptyState icon={CircleAlert} title="No recent activity" text="Workspace events will appear here as you work." /></section> }

function SchedulePanel({ campaigns, loading, error }: { campaigns: Campaign[]; loading: boolean; error: boolean }) { return <section className="dashboard-card"><CardHeader title="Upcoming Schedule" action={<Link to="/campaigns">View calendar <ArrowUpRight size={14} /></Link>} />{loading ? <SkeletonRows count={3} /> : error ? <CompactError /> : campaigns.length ? <div className="schedule-list">{campaigns.map((campaign) => <Link className="schedule-item" to={`/campaigns/${campaign.id}`} key={campaign.id}><time dateTime={campaign.scheduled_at ?? undefined}><b>{new Date(campaign.scheduled_at ?? 0).toLocaleDateString(undefined, { month: 'short' }).toUpperCase()}</b><strong>{new Date(campaign.scheduled_at ?? 0).getDate()}</strong></time><span><strong>{campaign.name}</strong><small>{new Date(campaign.scheduled_at ?? 0).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })} · {campaign.recipient_count.toLocaleString()} recipients</small></span><ChevronRight size={15} /></Link>)}</div> : <EmptyState icon={CalendarDays} title="Nothing scheduled" text="Approved campaigns with a future send time appear here." action="Create Campaign" href="/campaigns/new" />}</section> }

 type AttentionItem = { title: string; description: string; href: string; tone: string; icon: typeof AlertTriangle }
function buildAttention(report: { contacts: { invalid: number }; campaigns: { by_status: Record<string, number> }; senders: { needs_attention: number } }, rows: { campaign_id: string; campaign_name: string; status: string }[]): AttentionItem[] { const items: AttentionItem[] = []; if (report.senders.needs_attention) items.push({ title: 'Sender health needs review', description: `${report.senders.needs_attention} sender account${report.senders.needs_attention === 1 ? '' : 's'} need attention`, href: '/senders', tone: 'warning', icon: ShieldAlert }); if (report.contacts.invalid) items.push({ title: 'Contacts have issues', description: `${report.contacts.invalid.toLocaleString()} invalid contacts need review`, href: '/contacts', tone: 'warning', icon: Users }); const reviewCount = rows.filter((row) => ['REVIEW', 'PENDING_APPROVAL'].includes(row.status)).length; if (reviewCount) items.push({ title: 'Campaigns awaiting approval', description: `${reviewCount} campaign${reviewCount === 1 ? '' : 's'} require review`, href: '/campaigns', tone: 'info', icon: Megaphone }); return items }

function CardHeader({ title, subtitle, badge, action }: { title: string; subtitle?: string; badge?: number; action?: ReactNode }) { return <div className="dashboard-card-header"><div><h2>{title}{badge !== undefined && <span className="attention-count">{badge}</span>}</h2>{subtitle && <p>{subtitle}</p>}</div>{action}</div> }
function EmptyState({ icon: Icon, title, text, action, href }: { icon: typeof CheckCircle2; title: string; text: string; action?: string; href?: string }) { return <div className="dashboard-empty"><Icon size={22} /><strong>{title}</strong><span>{text}</span>{action && href && <Link className="small-primary" to={href}>{action}</Link>}</div> }
function SkeletonRows({ count }: { count: number }) { return <div className="skeleton-list">{Array.from({ length: count }, (_, index) => <i key={index} />)}</div> }
function CompactError() { return <div className="dashboard-inline-error"><CircleAlert size={17} />Unable to load schedule.</div> }
function DashboardSkeleton() { return <main className="production-dashboard"><div className="dashboard-loading-heading" /><section className="dashboard-kpis">{[1, 2, 3, 4].map((item) => <div className="dashboard-kpi skeleton" key={item} />)}</section><section className="dashboard-primary-grid"><div className="dashboard-card skeleton-card" /><div className="dashboard-card skeleton-card" /></section></main> }
function DashboardError({ onRetry }: { onRetry: () => void }) { return <main className="production-dashboard"><div className="dashboard-failure"><CircleAlert size={24} /><strong>Unable to load dashboard</strong><span>We couldn&apos;t load your workspace metrics.</span><button className="small-primary" onClick={onRetry}><RefreshCw size={14} />Retry</button></div></main> }

