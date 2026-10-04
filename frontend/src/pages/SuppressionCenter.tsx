import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Ban, Plus, Search, ShieldBan, Trash2 } from 'lucide-react'
import { suppressionApi, SUPPRESSION_TAB_LABELS, type SuppressionType } from '../api/suppression'

type Props = { accessToken: string }
type TabValue = '' | Exclude<SuppressionType, 'ADMIN_BLOCKED'>

export default function SuppressionCenter({ accessToken }: Props) {
  const client = useQueryClient()
  const [tab, setTab] = useState<TabValue>('')
  const [search, setSearch] = useState('')
  const [showAdd, setShowAdd] = useState(false)
  const [addEmail, setAddEmail] = useState('')
  const [addType, setAddType] = useState<'MANUAL' | 'ADMIN_BLOCKED'>('MANUAL')
  const [addReason, setAddReason] = useState('')

  const entries = useQuery({ queryKey: ['suppression-entries', tab, search], queryFn: () => suppressionApi.entries(tab, search, false, accessToken) })

  const remove = useMutation({
    mutationFn: (id: string) => suppressionApi.removeEntry(id, accessToken),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ['suppression-entries'] }) },
    onError: (error: Error) => window.alert(error.message),
  })

  const add = useMutation({
    mutationFn: () => suppressionApi.addEntry({ email: addEmail, type: addType, reason: addReason || undefined }, accessToken),
    onSuccess: () => { setShowAdd(false); setAddEmail(''); setAddReason(''); void client.invalidateQueries({ queryKey: ['suppression-entries'] }) },
    onError: (error: Error) => window.alert(error.message),
  })

  const counts = (type: TabValue) => {
    const list = entries.data ?? []
    return type === '' ? list.length : list.filter((item) => item.type === type).length
  }

  return (
    <main className="contact-form-page">
      <header className="form-heading"><div><p className="eyebrow">COMPLIANCE / GLOBAL SAFETY CONTROL</p><h1>Suppression center</h1><p className="muted">Every outgoing message is checked against these records before delivery.</p></div><button className="outline-button" onClick={() => setShowAdd((value) => !value)}><Plus size={16} /> Add suppression</button></header>

      {showAdd && (
        <section className="suppression-add-panel">
          <label className="form-field"><span>Email</span><input value={addEmail} onChange={(event) => setAddEmail(event.target.value)} placeholder="recipient@example.com" /></label>
          <label className="form-field"><span>Type</span><select value={addType} onChange={(event) => setAddType(event.target.value as 'MANUAL' | 'ADMIN_BLOCKED')}><option value="MANUAL">Manual</option><option value="ADMIN_BLOCKED">Admin blocked</option></select></label>
          <label className="form-field"><span>Reason</span><input value={addReason} onChange={(event) => setAddReason(event.target.value)} placeholder="Why is this recipient suppressed?" /></label>
          <button className="primary-button" type="button" disabled={add.isPending || !addEmail.trim()} onClick={() => add.mutate()}>{add.isPending ? 'Adding...' : 'Add suppression'}</button>
        </section>
      )}

      <section className="senders-tabs suppression-tabs" role="tablist">
        {SUPPRESSION_TAB_LABELS.map((item) => (
          <button key={item.value} type="button" role="tab" aria-selected={tab === item.value} className={`tab-button ${tab === item.value ? 'active' : ''}`} onClick={() => setTab(item.value as TabValue)}>{item.label}<span className="tab-count">{counts(item.value as TabValue)}</span></button>
        ))}
      </section>

      <div className="search-field suppression-search"><Search size={16} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search email or reason" aria-label="Search suppression" /></div>

      <div className="suppression-summary"><Ban size={18} /><strong>{counts(tab)}</strong><span>{tab === '' ? 'recipients suppressed' : `${tab.replaceAll('_', ' ').toLowerCase()} recipients`}</span></div>

      {entries.isLoading ? <div className="table-state">Loading suppression records...</div>
        : entries.isError ? <div className="table-state error-state">{entries.error.message}</div>
        : !entries.data?.length ? <div className="table-state">No suppression records for this view.</div>
        : (
          <div className="contact-table-wrap"><table className="contact-table suppression-table"><thead><tr><th>Email</th><th>Type</th><th>Source</th><th>Provider</th><th>Reason</th><th>Date</th><th aria-label="Actions" /></tr></thead><tbody>
            {entries.data.map((item) => (
              <tr key={item.id}><td><strong>{item.email}</strong></td><td><span className={`status-badge ${item.protected ? 'inactive' : 'active'}`}>{item.type.replaceAll('_', ' ')}</span></td><td>{item.source}</td><td>{item.provider ?? '—'}</td><td>{item.reason ?? '—'}</td><td>{item.created_at ? new Date(item.created_at).toLocaleDateString() : '—'}</td><td><button className="icon-button row-delete" type="button" disabled={item.protected} title={item.protected ? 'Provider-derived suppression cannot be removed' : 'Remove suppression'} aria-label="Remove suppression" onClick={() => { if (window.confirm(`Remove suppression for ${item.email}?`)) remove.mutate(item.id) }}><Trash2 size={16} /></button></td></tr>
            ))}
          </tbody></table>
          {entries.data.some((item) => item.protected) && <p className="muted suppression-protection-note"><ShieldBan size={14} /> Provider-derived records (complaints, hard bounces, unsubscribes) cannot be removed.</p>}
          </div>
        )}
    </main>
  )
}
