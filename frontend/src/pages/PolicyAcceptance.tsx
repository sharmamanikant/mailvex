import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, FileText, ShieldCheck } from 'lucide-react'
import { policyApi, type PolicyStatus } from '../api/client'

export default function PolicyAcceptance({ accessToken }: { accessToken: string }) {
  const queryClient = useQueryClient()
  const [expanded, setExpanded] = useState<string | null>(null)
  const status = useQuery({ queryKey: ['policy-status'], queryFn: () => policyApi.status(accessToken) })
  const document = useQuery({
    queryKey: ['policy-document', expanded],
    queryFn: () => policyApi.document(expanded as string, accessToken),
    enabled: Boolean(expanded),
  })
  const accept = useMutation({
    mutationFn: (policyType: string) => policyApi.accept(policyType, accessToken),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['policy-status'] }),
  })

  if (status.isLoading) return <main className="page-content"><div className="table-state">Loading policy status...</div></main>
  if (status.isError) return <main className="page-content"><div className="table-state error-state" role="alert">Unable to load policy status: {status.error.message}</div></main>

  const documents = status.data?.documents ?? []
  return (
    <main className="page-content policy-page">
      <div className="page-heading">
        <div><p className="eyebrow">WORKSPACE POLICIES</p><h1>Policy acceptance</h1><p className="muted">Review the current platform policies and record your workspace acceptance.</p></div>
        <span className="compliance-lock"><ShieldCheck size={17} /> AUDITABLE</span>
      </div>
      {!status.data?.required && <div className="notice-panel"><CheckCircle2 size={18} /> Policy acceptance is currently optional for this workspace.</div>}
      <section className="policy-list">
        {documents.map((item) => <PolicyCard key={item.policy_type} item={item} expanded={expanded === item.policy_type} onToggle={() => setExpanded(expanded === item.policy_type ? null : item.policy_type)} onAccept={() => accept.mutate(item.policy_type)} pending={accept.isPending && accept.variables === item.policy_type} error={accept.isError && accept.variables === item.policy_type ? accept.error.message : null} document={expanded === item.policy_type ? document.data : undefined} documentLoading={expanded === item.policy_type && document.isLoading} />)}
      </section>
    </main>
  )
}

function PolicyCard({ item, expanded, onToggle, onAccept, pending, error, document, documentLoading }: { item: PolicyStatus; expanded: boolean; onToggle: () => void; onAccept: () => void; pending: boolean; error: string | null; document?: { body: string }; documentLoading: boolean }) {
  return <article className={`policy-card ${item.accepted ? 'accepted' : 'needs-acceptance'}`}>
    <div className="policy-card-heading"><div className="policy-card-title"><FileText size={19} /><div><p className="eyebrow">VERSION {item.policy_version}</p><h2>{item.title}</h2></div></div><span className={`status-pill ${item.accepted ? '' : 'status-warning'}`}><span />{item.accepted ? 'Accepted' : 'Action needed'}</span></div>
    <p className="muted">{item.summary}</p>
    {item.accepted_at && <small className="field-help">Accepted on {new Date(item.accepted_at).toLocaleString()}</small>}
    <div className="policy-card-actions"><button className="outline-button" onClick={onToggle}>{expanded ? 'Hide policy' : 'Read policy'}</button>{!item.accepted && <button className="primary-button" onClick={onAccept} disabled={pending}>{pending ? 'Recording...' : 'Accept policy'}</button>}</div>
    {error && <p className="form-error" role="alert">{error}</p>}
    {expanded && <div className="policy-document">{documentLoading ? <div className="table-state">Loading document...</div> : <pre>{document?.body}</pre>}</div>}
  </article>
}
