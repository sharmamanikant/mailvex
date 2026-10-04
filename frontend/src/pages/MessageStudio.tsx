import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Check, FileInput, Pencil, RefreshCw, Sparkles, WandSparkles, X } from 'lucide-react'
import { draftsApi, type AIMessageDraft, type DraftGenerateInput } from '../api/drafts'
import { contactsApi } from '../api/contacts'
import { sendersApi } from '../api/senders'
import type { Contact } from '../types/contacts'
import type { Sender } from '../types/senders'

const GENERATION_TYPES = ['INITIAL_EMAIL', 'FOLLOW_UP', 'MEETING_REQUEST', 'SERVICE_INTRO', 'JOB_REQUIREMENT', 'CANDIDATE_INTRO', 'INFO_REQUEST', 'EVENT_INVITATION', 'THANK_YOU', 'GENERAL_OUTREACH']
const TONES = ['PROFESSIONAL', 'FRIENDLY', 'CONCISE', 'CONSULTATIVE', 'FORMAL', 'TECHNICAL', 'RECRUITMENT', 'SALES', 'NEUTRAL']
const LANGUAGES = ['English', 'Spanish', 'French', 'German', 'Portuguese']
const LENGTHS = ['SHORT', 'MEDIUM', 'LONG']
const REVIEWABLE = ['GENERATED', 'REVIEW_REQUIRED']
const EDITABLE = ['GENERATED', 'REVIEW_REQUIRED', 'REJECTED', 'FAILED']

function humanize(value: string): string {
  return value.toLowerCase().replace(/_/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase())
}

type Recipient = { first_name: string; last_name: string; email: string; company: string; designation: string; location: string; industry: string }
const emptyRecipient: Recipient = { first_name: '', last_name: '', email: '', company: '', designation: '', location: '', industry: '' }

const FACT_KEYS = ['name', 'company', 'designation', 'location'] as const
const FACT_LABELS: Record<string, string> = { name: 'Mutual contact', company: 'Company', designation: 'Role / title', location: 'Location / city' }

function statusInfo(status: string): { text: string; className: string; short: string } {
  switch (status) {
    case 'APPROVED': return { text: 'APPROVED', className: 'approved', short: 'APPROVED' }
    case 'REJECTED': return { text: 'REJECTED', className: 'rejected', short: 'REJECTED' }
    case 'FAILED': return { text: 'FAILED · RETRY', className: 'failed', short: 'FAILED' }
    case 'REVIEW_REQUIRED': return { text: 'REVIEW REQUIRED · FILL MISSING FACTS', className: 'review', short: 'REVIEW REQUIRED' }
    case 'GENERATED': return { text: 'GENERATED · READY FOR REVIEW', className: 'generated', short: 'GENERATED' }
    case 'GENERATING': return { text: 'GENERATING', className: 'generating', short: 'GENERATING' }
    default: return { text: status, className: '', short: status }
  }
}

export default function MessageStudio({ accessToken }: { accessToken: string }) {
  const [form, setForm] = useState<DraftGenerateInput>({ objective: '', audience: '', context: '', service: '', product: '', cta: '', tone: 'PROFESSIONAL', language: 'English', desired_length: 'MEDIUM', generation_type: 'INITIAL_EMAIL', sender: { sender_name: '' }, recipient: {}, custom_values: {} })
  const [recipient, setRecipient] = useState<Recipient>(emptyRecipient)
  const [facts, setFacts] = useState<Record<string, string>>({})
  const [draft, setDraft] = useState<AIMessageDraft | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [newTone, setNewTone] = useState('FORMAL')
  const [newLanguage, setNewLanguage] = useState('Spanish')
  const [note, setNote] = useState('')
  const [templateName, setTemplateName] = useState('')
  const [editedSubject, setEditedSubject] = useState('')
  const [editedBody, setEditedBody] = useState('')
  const [contacts, setContacts] = useState<Contact[]>([])
  const [senders, setSenders] = useState<Sender[]>([])

  useEffect(() => {
    contactsApi.list(new URLSearchParams({ page_size: '50' }), accessToken).then((page) => setContacts(page.items)).catch(() => undefined)
    sendersApi.list(accessToken).then(setSenders).catch(() => undefined)
  }, [accessToken])

  useEffect(() => { if (draft) { setEditedSubject(draft.generated_subject); setEditedBody(draft.generated_body) } }, [draft])

  const { data: usage } = useQuery({ queryKey: ['draft-usage'], queryFn: () => draftsApi.usage(accessToken), enabled: Boolean(accessToken) })
  const { data: history } = useQuery({ queryKey: ['drafts'], queryFn: () => draftsApi.list(accessToken), enabled: Boolean(accessToken) })

  const onError = (err: unknown) => setError(err instanceof Error ? err.message : 'Request failed')
  const sync = (next: AIMessageDraft) => { setDraft(next); setError(null) }

  const generate = useMutation({ mutationFn: (input: DraftGenerateInput) => draftsApi.create(input, accessToken), onSuccess: sync, onError })
  const transform = useMutation({ mutationFn: (action: string) => draftsApi.transform(draft!.id, { action, tone: newTone, language: newLanguage }, accessToken), onSuccess: sync, onError })
  const approve = useMutation({ mutationFn: () => draftsApi.approve(draft!.id, note, accessToken), onSuccess: sync, onError })
  const reject = useMutation({ mutationFn: () => draftsApi.reject(draft!.id, note, accessToken), onSuccess: sync, onError })
  const saveTemplate = useMutation({ mutationFn: () => draftsApi.saveAsTemplate(draft!.id, templateName || `Approved draft ${new Date().toLocaleDateString()}`, accessToken), onSuccess: sync, onError })
  const saveEdit = useMutation({ mutationFn: () => draftsApi.update(draft!.id, { subject: editedSubject, body: editedBody }, accessToken), onSuccess: sync, onError })

  const buildInput = (): DraftGenerateInput => {
    const input = { ...form, recipient: { ...recipient }, sender: { ...form.sender }, custom_values: { ...facts } }
    if (!input.contact_id) {
      const matched = contacts.find((contact) => contact.email === recipient.email)
      if (matched) input.contact_id = matched.id
    }
    return input
  }

  const set = (key: keyof DraftGenerateInput, value: string) => setForm((current) => ({ ...current, [key]: value }))
  const setRecipientField = (key: keyof Recipient, value: string) => setRecipient((current) => ({ ...current, [key]: value }))
  const setFact = (key: string, value: string) => setFacts((current) => ({ ...current, [key]: value }))
  const act = (mutation: { mutate: () => void; isPending: boolean }) => { if (!mutation.isPending) { setError(null); mutation.mutate() } }
  const actTransform = (action: string) => { if (!transform.isPending) { setError(null); transform.mutate(action) } }

  const pickContact = (contactId: string) => {
    if (!contactId) return
    const contact = contacts.find((item) => item.id === contactId)
    if (!contact) return
    setRecipient({ first_name: contact.first_name ?? '', last_name: contact.last_name ?? '', email: contact.email, company: contact.company ?? '', designation: contact.designation ?? '', location: contact.location ?? '', industry: contact.industry ?? '' })
    setForm((current) => ({ ...current, contact_id: contactId }))
  }

  const status = draft ? statusInfo(draft.generation_status) : null

  return (
    <main className="studio-page">
      <div className="studio-heading">
        <div>
          <p className="eyebrow">AI MESSAGE STUDIO</p>
          <h1>Start with the objective.</h1>
          <p className="muted">Give the assistant verified context. Every result enters the workspace as a draft.</p>
        </div>
        <span className="draft-pill"><span />DRAFT ONLY</span>
      </div>

      {error && <div className="form-error" style={{ marginBottom: 20 }}>{error}</div>}

      <div className="studio-layout">
        <section className="studio-form">
          <Field label="Objective" value={form.objective ?? ''} onChange={(value) => set('objective', value)} placeholder="What should this email achieve?" textarea />
          <Field label="Audience" value={form.audience ?? ''} onChange={(value) => set('audience', value)} placeholder="Who is receiving this?" />

          <div className="studio-subsection">
            <div className="section-heading"><strong>What are you offering?</strong><span>Use only verified facts. Unverified details are never invented.</span></div>
            <Field label="Service" value={form.service ?? ''} onChange={(value) => set('service', value)} placeholder="e.g. AWS cost optimization" />
            <Field label="Product (optional)" value={form.product ?? ''} onChange={(value) => set('product', value)} placeholder="e.g. InfraCost dashboard" />
          </div>

          <Field label="Context" value={form.context ?? ''} onChange={(value) => set('context', value)} placeholder="Facts the assistant may reference: prior conversations, goals, timeline" textarea />

          <div className="studio-subsection">
            <div className="section-heading"><strong>Custom facts</strong><span>Free-text personalization values. Always treated as data, never as instructions.</span></div>
            {FACT_KEYS.map((key) => (
              <Field key={key} label={FACT_LABELS[key]} value={facts[key] ?? ''} onChange={(value) => setFact(key, value)} placeholder={`e.g. ${key === 'name' ? 'Raj Sharma' : key === 'company' ? 'Acme' : key === 'designation' ? 'CTO' : 'New York'}`} />
            ))}
          </div>

          <div className="studio-row">
            <label className="form-field"><span>Generation type</span><select value={form.generation_type} onChange={(event) => set('generation_type', event.target.value)}>{GENERATION_TYPES.map((item) => <option key={item} value={item}>{humanize(item)}</option>)}</select></label>
            <label className="form-field"><span>Desired length</span><select value={form.desired_length} onChange={(event) => set('desired_length', event.target.value)}>{LENGTHS.map((item) => <option key={item} value={item}>{humanize(item)}</option>)}</select></label>
          </div>
          <div className="studio-row">
            <label className="form-field"><span>Tone</span><select value={form.tone} onChange={(event) => set('tone', event.target.value)}>{TONES.map((item) => <option key={item} value={item}>{humanize(item)}</option>)}</select></label>
            <label className="form-field"><span>Language</span><select value={form.language} onChange={(event) => set('language', event.target.value)}>{LANGUAGES.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
          </div>

          <Field label="Call to action" value={form.cta ?? ''} onChange={(value) => set('cta', value)} placeholder="What should the reader do next?" />

          <div className="studio-subsection">
            <div className="section-heading"><strong>Sender</strong><span>Pick an authorized sender, or type sender facts directly.</span></div>
            <label className="form-field"><span>Sender account</span><select value={form.sender_id ?? ''} onChange={(event) => set('sender_id', event.target.value)}><option value="">None selected</option>{senders.map((sender) => <option key={sender.id} value={sender.id}>{sender.email}</option>)}</select></label>
            <Field label="Sender name" value={form.sender?.sender_name ?? ''} onChange={(value) => setForm((current) => ({ ...current, sender: { ...current.sender, sender_name: value } }))} placeholder="Your name" />
          </div>

          <div className="studio-subsection">
            <div className="section-heading"><strong>Recipient</strong><span>Pick a contact to pull verified details for the draft, or type them manually.</span></div>
            <label className="form-field"><span>Contact</span><select value={contacts.find((item) => item.id === form.contact_id)?.id ?? ''} onChange={(event) => pickContact(event.target.value)}><option value="">Type recipient details manually</option>{contacts.map((contact) => <option key={contact.id} value={contact.id}>{contact.email} · {contact.first_name ?? ''} {contact.company ?? ''}</option>)}</select></label>
            <div className="recipient-grid">
              <Field label="First name" value={recipient.first_name} onChange={(value) => setRecipientField('first_name', value)} />
              <Field label="Last name" value={recipient.last_name} onChange={(value) => setRecipientField('last_name', value)} />
            </div>
            <Field label="Email" value={recipient.email} onChange={(value) => setRecipientField('email', value)} />
            <div className="recipient-grid">
              <Field label="Company" value={recipient.company} onChange={(value) => setRecipientField('company', value)} />
              <Field label="Designation" value={recipient.designation} onChange={(value) => setRecipientField('designation', value)} />
            </div>
          </div>

          <button className="primary-button studio-generate" disabled={generate.isPending} onClick={() => { setError(null); generate.mutate(buildInput()) }}>
            <Sparkles size={16} />{generate.isPending ? 'Generating draft…' : 'Generate email draft'} <span>-&gt;</span>
          </button>
        </section>

        <section className="studio-result">
          {draft && status ? (
            <>
              <div className="result-heading">
                <div>
                  <p className="eyebrow">DRAFT #{draft.id.slice(0, 8)}</p>
                  {EDITABLE.includes(draft.generation_status)
                    ? <input className="subject-input" value={editedSubject} onChange={(event) => setEditedSubject(event.target.value)} aria-label="Subject" />
                    : <h2>{draft.generated_subject}</h2>}
                </div>
                <span className={`ai-state ${status.className}`}>{status.text}</span>
              </div>

              {draft.warnings.length > 0 && (
                <div className="warning-list">
                  {draft.warnings.map((warning) => <span key={warning}><X size={13} />{warning}</span>)}
                </div>
              )}

              {EDITABLE.includes(draft.generation_status)
                ? <textarea className="studio-textarea message-edit" value={editedBody} onChange={(event) => setEditedBody(event.target.value)} aria-label="Email body" />
                : <div className="message-body">{editedBody.split('\n').map((line, index) => <p key={index}>{line}</p>)}</div>}

              <div className="result-cta">
                <small>META</small>
                <span className="meta-line">{draft.provider} {draft.model ?? ''} · {draft.generation_type} · {draft.tone} · {draft.language} · {draft.desired_length}</span>
                <span className="meta-line">tokens {draft.total_tokens ?? '–'} · ${(Number(draft.estimated_cost) || 0).toFixed(6)} {draft.cost_currency} · {draft.request_duration_ms ?? '–'}ms</span>
              </div>

              <div className="transform-actions">
                <button className="outline-button compact" onClick={() => actTransform('regenerate')} disabled={transform.isPending}><RefreshCw size={13} /> Regenerate</button>
                <button className="outline-button compact" onClick={() => actTransform('improve')} disabled={transform.isPending}><WandSparkles size={13} /> Improve</button>
                <button className="outline-button compact" onClick={() => actTransform('shorten')} disabled={transform.isPending}><WandSparkles size={13} /> Shorten</button>
                <button className="outline-button compact" onClick={() => actTransform('expand')} disabled={transform.isPending}><WandSparkles size={13} /> Expand</button>
              </div>

              <div className="transform-row">
                <select value={newTone} onChange={(event) => setNewTone(event.target.value)} aria-label="Target tone"><option value="">Tone target…</option>{TONES.map((item) => <option key={item} value={item}>{humanize(item)}</option>)}</select>
                <span className="tone-badge">{draft.tone}</span>
                <button className="outline-button compact" onClick={() => actTransform('change_tone')} disabled={transform.isPending}>Change tone</button>
                <label className="transform-language"><input type="text" value={newLanguage} onChange={(event) => setNewLanguage(event.target.value)} aria-label="Target language" /></label>
                <button className="outline-button compact" onClick={() => actTransform('translate')} disabled={transform.isPending}>Translate</button>
              </div>

              <div className="review-actions">
                <input className="note-input" value={note} onChange={(event) => setNote(event.target.value)} placeholder={REVIEWABLE.includes(draft.generation_status) ? 'Review note (optional)' : 'Draft note'} />
                {REVIEWABLE.includes(draft.generation_status) && (
                  <>
                    <button className="primary-button compact" onClick={() => act(approve)} disabled={approve.isPending}><Check size={14} /> Approve</button>
                    <button className="danger-button compact" onClick={() => act(reject)} disabled={reject.isPending}>Reject</button>
                  </>
                )}
                {EDITABLE.includes(draft.generation_status) && (
                  <button className="outline-button compact" onClick={() => act(saveEdit)} disabled={saveEdit.isPending}><Pencil size={13} /> Update subject &amp; body</button>
                )}
                {draft.generation_status === 'APPROVED' && (
                  <button className="primary-button compact" onClick={() => act(saveTemplate)} disabled={saveTemplate.isPending}><FileInput size={14} /> Save as template</button>
                )}
              </div>

              {draft.generation_status === 'APPROVED' && (
                <div className="studio-subsection">
                  <div className="section-heading"><strong>Reuse in templates</strong><span>Approved content can be saved as a versioned Template for campaigns.</span></div>
                  <input className="note-input" value={templateName} onChange={(event) => setTemplateName(event.target.value)} placeholder="Template name (e.g. Initial outreach — Acme)" />
                </div>
              )}

              {draft.review_note && <div className="review-note">{draft.review_note}</div>}
            </>
          ) : (
            <div className="studio-empty">
              <Sparkles size={34} />
              <h2>No draft yet.</h2>
              <p>Fill in the objective and verified facts, then generate. Drafts never leave the workspace without human approval.</p>
            </div>
          )}

          <div className="usage-card">
            <small>MONTHLY AI USAGE</small>
            <div className="usage-row"><span>Generations</span><strong>{usage?.monthly_generations ?? '–'}{usage?.monthly_generation_limit ? ` / ${usage.monthly_generation_limit}` : ''}</strong></div>
            <div className="usage-row"><span>Estimated cost</span><strong>${Number(usage?.monthly_cost_usd ?? 0).toFixed(6)} USD</strong></div>
            {usage?.monthly_budget_usd && Number(usage.monthly_budget_usd) > 0 && <div className="usage-row"><span>Monthly budget</span><strong>${Number(usage.monthly_budget_usd).toFixed(2)}</strong></div>}
          </div>

          {history && history.items.length > 0 && (
            <div className="draft-history">
              <small>RECENT DRAFTS</small>
              {history.items.map((item) => {
                const pill = statusInfo(item.generation_status)
                return (
                  <button key={item.id} className="draft-history-row" onClick={() => draftsApi.get(item.id, accessToken).then(setDraft).catch(() => undefined)}>
                    <span>{item.generated_subject || 'Untitled'}</span>
                    <em className={`ai-state ${pill.className}`}>{pill.short}</em>
                  </button>
                )
              })}
            </div>
          )}
        </section>
      </div>
    </main>
  )
}

function Field({ label, value, onChange, placeholder, textarea }: { label: string; value: string; onChange: (value: string) => void; placeholder?: string; textarea?: boolean }) {
  return (
    <label className="form-field">
      <span>{label}</span>
      {textarea
        ? <textarea className="studio-textarea" value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} />
        : <input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} />}
    </label>
  )
}