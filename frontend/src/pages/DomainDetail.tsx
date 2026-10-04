import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, CircleAlert, Globe, RefreshCw, ShieldCheck } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'
import { domainsApi } from '../api/domains'

const statusClass = (status: string) => (status === 'PASS' ? 'pass' : status === 'FAIL' ? 'fail' : status === 'WARNING' ? 'warning' : 'unknown')

export default function DomainDetail({ accessToken }: { accessToken: string }) {
  const { id } = useParams()
  const client = useQueryClient()
  const health = useQuery({ queryKey: ['domain-health', id], queryFn: () => domainsApi.getHealth(id!, accessToken) })
  const history = useQuery({ queryKey: ['domain-history', id], queryFn: () => domainsApi.history(id!, accessToken) })
  const check = useMutation({ mutationFn: () => domainsApi.checkHealth(id!, accessToken), onSuccess: () => { void client.invalidateQueries({ queryKey: ['domain-health', id] }); void client.invalidateQueries({ queryKey: ['domain-history', id] }) } })
  if (health.isLoading) return <main className="domain-detail-page"><Link to="/domains" className="back-link"><ArrowLeft size={16} /> Back to domains</Link><div className="table-state">Loading domain health...</div></main>
  if (health.isError || !health.data) return <main className="domain-detail-page"><Link to="/domains" className="back-link"><ArrowLeft size={16} /> Back to domains</Link><div className="table-state error-state">{health.error?.message ?? 'Domain not found'}</div></main>
  const data = health.data
  const Icon = data.status === 'PASS' ? ShieldCheck : data.status === 'FAIL' || data.status === 'UNKNOWN' ? CircleAlert : CircleAlert
  return <main className="domain-detail-page"><Link to="/domains" className="back-link"><ArrowLeft size={16} /> Back to domains</Link><div className="detail-heading"><div><p className="eyebrow">DOMAIN HEALTH</p><div className="detail-title"><div className={`domain-badge ${statusClass(data.status)}`}><Icon size={18} /></div><div><h1>{data.domain}</h1><p className="muted">{data.summary}</p></div></div></div><button className="outline-button" disabled={check.isPending} onClick={() => void check.mutate()}><RefreshCw size={15} /> Re-run check</button></div><div className="health-disclaimer">{data.disclaimer}</div><section className="detail-card"><h2>DNS checks</h2><div className="domain-checks">{Object.entries(data.checks).map(([name, check]) => <article className={`domain-check ${statusClass(check.status)}`} key={name}><div className="domain-check-head"><span className={`check-pill ${statusClass(check.status)}`}>{check.status}</span><strong>{name}</strong></div><p>{check.message}</p><small>Remediation: {check.remediation}</small></article>)}</div></section><section className="detail-card"><h2>Remediation</h2><p className="muted">{data.remediation}</p></section><section className="detail-card"><h2>History</h2>{history.data?.history.length ? <div className="domain-history">{history.data.history.map((entry, index) => <div className="domain-history-row" key={index}><span className={`check-pill ${statusClass(entry.status)}`}>{entry.status}</span><span>{new Date(entry.checked_at).toLocaleString()}</span><Globe size={14} /></div>)}</div> : <p className="muted">No checks have been recorded yet.</p>}</section></main>
}
