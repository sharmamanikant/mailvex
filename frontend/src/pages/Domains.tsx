import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { CircleAlert, Globe, Plus, ShieldCheck } from 'lucide-react'
import { domainsApi } from '../api/domains'
import type { DomainRecord } from '../api/senders'

const statusClass = (status: string) => (status === 'PASS' ? 'pass' : status === 'FAIL' ? 'fail' : status === 'WARNING' ? 'warning' : 'unknown')

export default function Domains({ accessToken }: { accessToken: string }) {
  const client = useQueryClient()
  const [name, setName] = useState('')
  const domains = useQuery({ queryKey: ['domains'], queryFn: () => domainsApi.list(accessToken) })
  const create = useMutation({ mutationFn: () => domainsApi.create(name, accessToken), onSuccess: () => { setName(''); void client.invalidateQueries({ queryKey: ['domains'] }) } })
  return <main className="domains-page"><div className="domains-heading"><div><p className="eyebrow">DOMAIN HEALTH</p><h1>Delivery domains</h1><p className="muted">SPF, DKIM, DMARC, and MX checks for the domains you send from.</p></div></div><div className="domain-add"><form onSubmit={(event) => { event.preventDefault(); void create.mutate() }}><input className="role-input" value={name} onChange={(event) => setName(event.target.value)} placeholder="Add a domain: example.com" /></form><button className="primary-button compact" disabled={!name || create.isPending} onClick={() => void create.mutate()}><Plus size={16} /> Add domain</button></div><section className="domain-note"><ShieldCheck size={17} /><span>These checks inspect DNS records. Passing does not guarantee inbox placement, which also depends on sender reputation and content.</span></section>{domains.isLoading ? <div className="table-state">Loading domains...</div> : domains.isError ? <div className="table-state error-state">{domains.error.message}</div> : domains.data?.domains.length ? <div className="domain-list">{domains.data.domains.map((domain: DomainRecord) => <Link to={`/domains/${domain.id}`} className="domain-card" key={domain.id}><div className="domain-icon"><Globe size={19} /></div><div className="domain-main"><div className="domain-title"><h2>{domain.domain}</h2><span className={`domain-status ${statusClass(domain.health_status)}`}><i />{domain.health_status}</span></div></div>{domain.health_status === 'FAIL' && <CircleAlert size={15} className="alert-icon" />}</Link>)}</div> : <div className="table-state">No domains added yet.</div>}</main>
}
