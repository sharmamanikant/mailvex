import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { AlertTriangle, Check, Download, Inbox as InboxIcon, Languages, Mail, MessageSquareText, RefreshCw, Sparkles, UserRound, Wand2, X } from 'lucide-react'
import { inboxApi } from '../api/inbox'
import { sendersApi } from '../api/senders'
import type { AssistantDraftResult, InboxMessage, InboxThread } from '../types/inbox'

const STATUSES = ['UNREAD', 'READ', 'REPLIED', 'ARCHIVED', 'REQUIRES_ACTION']

const INTENT_LABELS: Record<string, string> = {
  INTERESTED: 'Interested',
  NOT_INTERESTED: 'Not interested',
  REQUEST_FOR_INFORMATION: 'Request for information',
  REQUEST_FOR_MEETING: 'Request for meeting',
  PRICE_REQUEST: 'Price request',
  UNSUBSCRIBE: 'Unsubscribe request',
  OUT_OF_OFFICE: 'Out of office',
  WRONG_PERSON: 'Wrong person',
  UNKNOWN: 'Unknown',
}

const TONES = ['NEUTRAL', 'FRIENDLY', 'PROFESSIONAL', 'CONCISE', 'EMPATHETIC', 'CONFIDENT']

export type ActiveDraft = { id: string; subject: string; body: string } | null

export default function Inbox({ accessToken }: { accessToken: string }) {
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState('')
  const [senderId, setSenderId] = useState('')
  const [draft, setDraft] = useState<ActiveDraft>(null)
  const client = useQueryClient()

  useEffect(() => {
    setDraft(null)
  }, [selectedId])

  const list = useQuery({ queryKey: ['inbox-threads', statusFilter, senderId], queryFn: () => inboxApi.listThreads(accessToken, { status: statusFilter || undefined, senderId: senderId || undefined }) })
  const senders = useQuery({ queryKey: ['senders'], queryFn: () => sendersApi.list(accessToken) })
  const sync = useMutation({ mutationFn: (id: string) => inboxApi.sync(id, accessToken), onSuccess: () => { void client.invalidateQueries({ queryKey: ['inbox-threads'] }) } })

  return <main className="inbox-page">
    <div className="inbox-heading">
      <div><p className="eyebrow">UNIFIED INBOX</p><h1>Inbox</h1><p className="muted">Replies land here; associate, review, and reply from one place.</p></div>
      <div className="inbox-toolbar">
        <div className="inbox-filter"><InboxIcon size={15} /><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} aria-label="Filter by status"><option value="">All statuses</option>{STATUSES.map((s) => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}</select></div>
        <div className="inbox-filter"><RefreshCw size={15} /><select value={senderId} onChange={(event) => setSenderId(event.target.value)} aria-label="Filter by sender" disabled={senders.isLoading}><option value="">All senders</option>{senders.data?.filter((s) => s.status === 'CONNECTED').map((s) => <option key={s.id} value={s.id}>{s.email}</option>)}</select></div>
        {senderId && <button className="primary-button" onClick={() => sync.mutate(senderId)} disabled={sync.isPending}>{sync.isPending ? 'Syncing...' : 'Sync replies'}</button>}
      </div>
    </div>
    <div className="inbox-layout tri-pane">
      <aside className="thread-list">
        {list.isLoading ? <div className="table-state">Loading threads...</div> : list.isError ? <div className="table-state error-state">{list.error.message}</div> : list.data?.items.length ? list.data.items.map((thread) => <ThreadRow key={thread.id} thread={thread} selected={thread.id === selectedId} onClick={() => setSelectedId(thread.id)} />) : <div className="table-state"><Mail size={25} /><strong>No threads yet</strong><span>Select a sender and choose Sync replies to pull in messages.</span></div>}
      </aside>
      <section className="conversation-panel">{selectedId ? <ConversationView id={selectedId} accessToken={accessToken} onDraft={setDraft} /> : <div className="conversation-empty"><Mail size={28} /><h2>Select a thread</h2><p>Choose a conversation to review messages, add a reply, or mark it for action.</p></div>}</section>
      <aside className="recipient-panel">{selectedId ? <AssistantPanel id={selectedId} accessToken={accessToken} draft={draft} onDraft={setDraft} /> : <div className="recipient-empty"><Sparkles size={28} /><span>AI assistance and recipient details appear here.</span></div>}{selectedId ? <RecipientPanel id={selectedId} accessToken={accessToken} /> : null}</aside>
    </div>
  </main>
}

function ThreadRow({ thread, selected, onClick }: { thread: InboxThread; selected: boolean; onClick: () => void }) {
  const initial = (thread.contact_name?.[0] ?? thread.subject?.[0] ?? 'M').toUpperCase()
  const status = thread.status.replace('_', ' ').toLowerCase()
  const match = thread.match_status === 'MATCHED' ? '' : 'unmatched'
  return <button className={`thread-row ${selected ? 'selected' : ''}`} onClick={onClick}>
    <span className="thread-avatar">{initial}</span>
    <span className="thread-copy"><strong>{thread.subject || 'Untitled thread'}</strong><small>{thread.sender_email} · {thread.last_message_at ? new Date(thread.last_message_at).toLocaleString() : 'no timestamp'}</small><span className="thread-snippet">{thread.snippet}</span></span>
    <span className={`thread-status ${status} ${match}`} />
  </button>
}

function ConversationView({ id, accessToken, onDraft }: { id: string; accessToken: string; onDraft: (d: ActiveDraft) => void }) {
  const client = useQueryClient()
  const [subject, setSubject] = useState('Re: ')
  const [body, setBody] = useState('')
  const [draftId, setDraftId] = useState<string | null>(null)
  const detail = useQuery({ queryKey: ['inbox-thread', id], queryFn: () => inboxApi.getThread(id, accessToken) })

  const setStatus = useMutation({ mutationFn: (status: string) => inboxApi.setStatus(id, status, accessToken), onSuccess: () => void client.invalidateQueries({ queryKey: ['inbox-thread', id] }) })
  const draft = useMutation({ mutationFn: () => inboxApi.draft(id, accessToken), onSuccess: (result) => { setDraftId(result.draft_id); setSubject(result.subject); setBody(result.body); onDraft({ id: result.draft_id, subject: result.subject, body: result.body }) } })
  const send = useMutation({ mutationFn: () => inboxApi.send(id, subject, body, draftId, accessToken), onSuccess: () => { setBody(''); setDraftId(null); onDraft(null); void client.invalidateQueries({ queryKey: ['inbox-thread', id] }); void client.invalidateQueries({ queryKey: ['inbox-threads'] }) } })

  if (detail.isLoading) return <div className="table-state">Loading thread...</div>
  if (detail.isError || !detail.data) return <div className="table-state error-state">Unable to load thread.</div>
  const thread = detail.data
  return <div className="conversation-inner">
    <header className="conversation-header">
      <div className="conversation-title"><span className="thread-avatar large">{(thread.contact_name?.[0] ?? thread.subject?.[0] ?? 'M').toUpperCase()}</span><div><p className="eyebrow">{thread.match_status} · {thread.status}</p><h2>{thread.subject || 'Untitled thread'}</h2><p>{thread.sender_email || 'Sender unavailable'}{thread.campaign_name ? ` · ${thread.campaign_name}` : ''}</p></div></div>
      <select value={thread.status} onChange={(event) => void setStatus.mutate(event.target.value)} aria-label="Thread status">{STATUSES.map((s) => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}</select>
    </header>
    <div className="message-history">{thread.messages.map((message) => <MessageCard key={message.id} message={message} />)}</div>
    <div className="reply-composer">
      <div className="composer-label"><Sparkles size={14} /> Draft with AI — review &amp; send manually. Replies are never sent automatically.</div>
      <input className="composer-subject" value={subject} onChange={(event) => setSubject(event.target.value)} placeholder="Subject" aria-label="Reply subject" />
      <textarea className="composer-body" value={body} onChange={(event) => setBody(event.target.value)} placeholder="Write a reply..." aria-label="Reply body" rows={5} />
      <div className="composer-actions">
        <div className="composer-transform-row">
          <button className="outline-button" onClick={() => void draft.mutate()} disabled={draft.isPending} title="Draft a new reply with AI">{draft.isPending ? 'Drafting...' : <><Wand2 size={13} /> AI draft</>}</button>
          {draftId && <>
            <button className="outline-button" onClick={() => handleTransform('PROFESSIONAL')} title="Make reply more professional">Professional</button>
            <button className="outline-button" onClick={() => handleTransform('SHORTEN')} title="Shorten the reply">Shorten</button>
            <button className="outline-button" onClick={() => handleTransform('EXPAND')} title="Expand the reply">Expand</button>
          </>}
        </div>
        <button className="primary-button" onClick={() => void send.mutate()} disabled={send.isPending || !subject.trim() || !body.trim()}>{send.isPending ? 'Sending...' : 'Send reply'}</button>
      </div>
    </div>
  </div>

  function handleTransform(operation: string) {
    if (!draftId) return
    void inboxApi.transform(draftId, operation, accessToken).then((result) => { setDraftId(result.id); setSubject(result.subject); setBody(result.body); onDraft({ id: result.id, subject: result.subject, body: result.body }) })
  }
}

function MessageCard({ message }: { message: InboxMessage }) {
  const inbound = message.direction === 'INBOUND'
  return <article className={`reply-card ${inbound ? 'inbound' : 'outbound'}`}>
    <div className="reply-meta"><strong>{inbound ? message.from_email : message.to_email}</strong><small>{message.received_at ? new Date(message.received_at).toLocaleString() : ''} · {inbound ? 'Incoming' : 'Outgoing'}</small></div>
    {message.body_text && <p className="reply-body">{message.body_text}</p>}
  </article>
}

function AssistantPanel({ id, accessToken, draft, onDraft }: { id: string; accessToken: string; draft: ActiveDraft; onDraft: (d: ActiveDraft) => void }) {
  const client = useQueryClient()
  const [analysis, setAnalysis] = useState<{ intent: string; confidence: number; unsubscribe: boolean; suppressed: boolean; requires_confirmation: boolean; warnings: string[] } | null>(null)
  const [summary, setSummary] = useState<string | null>(null)
  const [nextAction, setNextAction] = useState<string | null>(null)
  const [tone, setTone] = useState('FRIENDLY')
  const [language, setLanguage] = useState('')
  const [confirmUnsubscribe, setConfirmUnsubscribe] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const analyze = useMutation({
    mutationFn: () => inboxApi.analyze(id, accessToken),
    onSuccess: (result) => { setAnalysis(result); setError(null); setConfirmUnsubscribe(result.unsubscribe) },
    onError: (err: Error) => setError(err.message),
  })
  const summarize = useMutation({
    mutationFn: () => inboxApi.summarize(id, accessToken),
    onSuccess: (result: AssistantDraftResult) => { setSummary(result.summary); setError(null) },
    onError: (err: Error) => setError(err.message),
  })
  const nextActionM = useMutation({
    mutationFn: () => inboxApi.nextAction(id, accessToken),
    onSuccess: (result: AssistantDraftResult) => { setNextAction(result.next_action); setError(null) },
    onError: (err: Error) => setError(err.message),
  })
  const generate = useMutation({
    mutationFn: () => inboxApi.assistantDraft(id, accessToken, confirmUnsubscribe),
    onSuccess: (result: AssistantDraftResult) => { onDraft({ id: result.id, subject: result.subject, body: result.body }); setError(null) },
    onError: (err: Error) => setError(err.message),
  })
  const approve = useMutation({
    mutationFn: () => inboxApi.approve(id, draft!.id, accessToken),
    onSuccess: () => { onDraft(null); setError(null); void client.invalidateQueries({ queryKey: ['inbox-thread', id] }); void client.invalidateQueries({ queryKey: ['inbox-threads'] }) },
    onError: (err: Error) => setError(err.message),
  })
  const reject = useMutation({
    mutationFn: () => inboxApi.rejectDraft(draft!.id, accessToken),
    onSuccess: () => { onDraft(null); setError(null) },
    onError: (err: Error) => setError(err.message),
  })

  return <div className="assistant-inner">
    <div className="assistant-heading"><span className="assistant-mark"><Sparkles size={15} /></span><div><p className="eyebrow">AI ASSISTANT</p><h3>Reply assistant</h3></div></div>

    <div className="assistant-actions">
      <button className="outline-button" onClick={() => void analyze.mutate()} disabled={analyze.isPending}>{analyze.isPending ? 'Analyzing...' : 'Identify intent'}</button>
      <button className="outline-button" onClick={() => void summarize.mutate()} disabled={summarize.isPending}>{summarize.isPending ? '...' : 'Summarize'}</button>
      <button className="outline-button" onClick={() => void nextActionM.mutate()} disabled={nextActionM.isPending}>{nextActionM.isPending ? '...' : 'Next action'}</button>
    </div>
    {error && <div className="assistant-error">{error}</div>}

    {analysis && <div className="assistant-card">
      <div className="assistant-card-title"><MessageSquareText size={13} /> Intent</div>
      <div className="assistant-intent">{INTENT_LABELS[analysis.intent] ?? analysis.intent} <span className="confidence">{(analysis.confidence * 100).toFixed(0)}%</span></div>
      {analysis.suppressed && <div className="assistant-warning"><AlertTriangle size={13} /> Suppressed recipient — do not send.</div>}
      {analysis.unsubscribe && <div className="assistant-warning warning-strong"><AlertTriangle size={13} /> Unsubscribe requested. A draft is blocked until you confirm, and no further email should be sent.</div>}
      {analysis.warnings.length > 0 && <ul className="assistant-warning-list">{analysis.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
    </div>}

    {summary && <div className="assistant-card"><div className="assistant-card-title"><Download size={13} /> Summary</div><p className="assistant-text">{summary}</p></div>}
    {nextAction && <div className="assistant-card"><div className="assistant-card-title"><MessageSquareText size={13} /> Suggested next action</div><p className="assistant-text">{nextAction}</p></div>}

    {analysis?.unsubscribe && <label className="assistant-check"><input type="checkbox" checked={confirmUnsubscribe} onChange={(event) => setConfirmUnsubscribe(event.target.checked)} /> I confirm this unsubscribe request</label>}

    <button className="primary-button assistant-generate" onClick={() => void generate.mutate()} disabled={generate.isPending || (analysis?.unsubscribe && !confirmUnsubscribe)}>{generate.isPending ? 'Generating...' : 'Generate reply draft'}</button>

    {draft && <div className="assistant-card draft-card">
      <div className="assistant-card-title"><Wand2 size={13} /> Refine draft</div>
      <div className="refine-buttons">
        <button className="outline-button" onClick={() => refine('PROFESSIONAL')}>Professional</button>
        <button className="outline-button" onClick={() => refine('SHORTEN')}>Shorten</button>
        <button className="outline-button" onClick={() => refine('EXPAND')}>Expand</button>
      </div>
      <div className="refine-row">
        <select value={tone} onChange={(event) => setTone(event.target.value)} aria-label="Tone">{TONES.map((t) => <option key={t} value={t}>{t[0] + t.slice(1).toLowerCase()}</option>)}</select>
        <button className="outline-button" onClick={() => refine('CHANGE_TONE', { tone })}>Apply tone</button>
      </div>
      <div className="refine-row">
        <input value={language} onChange={(event) => setLanguage(event.target.value)} placeholder="e.g. fr, es, de" aria-label="Target language" />
        <button className="outline-button" onClick={() => refine('TRANSLATE', { language })}><Languages size={12} /> Translate</button>
      </div>
      <div className="assistant-approve-actions">
        <button className="primary-button" onClick={() => void approve.mutate()} disabled={approve.isPending}>{approve.isPending ? '...' : <><Check size={13} /> Approve</>}</button>
        <button className="danger-button" onClick={() => void reject.mutate()} disabled={reject.isPending}>{reject.isPending ? '...' : <><X size={13} /> Reject</>}</button>
      </div>
    </div>}
  </div>

  function refine(operation: string, opts: { tone?: string; language?: string } = {}) {
    if (!draft) return
    void inboxApi.transform(draft.id, operation, accessToken, { tone: opts.tone ?? null, language: opts.language ? opts.language : null }).then((result) => { onDraft({ id: result.id, subject: result.subject, body: result.body }); setError(null) }).catch((err: Error) => setError(err.message))
  }
}

function RecipientPanel({ id, accessToken }: { id: string; accessToken: string }) {
  const detail = useQuery({ queryKey: ['inbox-thread', id], queryFn: () => inboxApi.getThread(id, accessToken) })
  if (detail.isLoading) return <div className="recipient-empty">Loading recipient...</div>
  if (detail.isError || !detail.data) return <div className="recipient-empty">Recipient unavailable.</div>
  const recipient = detail.data.recipient
  if (!recipient) return <div className="recipient-empty"><UserRound size={26} /><span>Not yet matched to a contact.</span></div>
  const Row = ({ label, value }: { label: string; value: string | null }) => <div className="recipient-row"><span>{label}</span><strong>{value || '—'}</strong></div>
  return <div className="recipient-inner">
    <div className="recipient-heading"><span className="thread-avatar large">{(recipient.name?.[0] ?? 'C').toUpperCase()}</span><div><p className="eyebrow">RECIPIENT</p><h3>{recipient.name}</h3><a href={`mailto:${recipient.email}`}>{recipient.email}</a></div></div>
    <div className="inbox-recipient-grid">
      <Row label="Company" value={recipient.company} />
      <Row label="Designation" value={recipient.designation} />
      <Row label="Campaign" value={recipient.campaign_name} />
      <Row label="Last contact" value={recipient.last_contact ? new Date(recipient.last_contact).toLocaleDateString() : null} />
      <Row label="Email" value={recipient.email} />
      <Row label="Contact ID" value={recipient.contact_id} />
    </div>
    {detail.data.campaign_name && recipient.campaign_id && <button className="outline-button" onClick={() => window.location.assign(`/campaigns/${recipient.campaign_id}`)}>Open campaign</button>}
  </div>
}
