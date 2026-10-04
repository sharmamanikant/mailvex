import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ChevronRight, ListFilter, Play, Plus, Trash2 } from 'lucide-react'
import { contactsApi } from '../api/contacts'
import type { ContactSegment, SegmentCondition, SegmentOperator } from '../types/contacts'

const SCALAR_FIELDS = ['first_name', 'last_name', 'email', 'phone', 'company', 'designation', 'location', 'website', 'industry', 'source', 'source_reference', 'status']
const OPERATORS: SegmentOperator[] = ['eq', 'neq', 'contains', 'not_contains', 'gt', 'gte', 'lt', 'lte', 'in', 'not_in', 'is_empty', 'is_not_empty']
const EMPTY_OPERATORS: SegmentOperator[] = ['is_empty', 'is_not_empty']
const LIST_OPERATORS: SegmentOperator[] = ['in', 'not_in']
const NUMERIC_OPERATORS: SegmentOperator[] = ['gt', 'gte', 'lt', 'lte']

type Row = { field: string; operator: SegmentOperator; value: string }

export default function Segments({ accessToken }: { accessToken: string }) {
  const client = useQueryClient()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [match, setMatch] = useState<'all' | 'any'>('all')
  const [rows, setRows] = useState<Row[]>([{ field: 'email', operator: 'contains', value: '' }])
  const [editingId, setEditingId] = useState<string | null>(null)
  const [activeId, setActiveId] = useState<string | null>(null)
  const [error, setError] = useState('')
  const segments = useQuery({ queryKey: ['segments'], queryFn: () => contactsApi.segments(accessToken) })
  const fields = useQuery({ queryKey: ['contact-fields'], queryFn: () => contactsApi.fieldDefinitions(accessToken) })
  const fieldOptions = [...SCALAR_FIELDS, ...(fields.data ?? []).map((field) => `custom.${field.key}`)]
  const members = useQuery({ queryKey: ['segments', activeId, 'members'], queryFn: () => contactsApi.segmentContacts(activeId!, new URLSearchParams({ page: '1', page_size: '50' }), accessToken), enabled: Boolean(activeId) })
  const buildConditions = (): SegmentCondition[] => rows.map((row) => {
    const value = row.value.trim()
    if (EMPTY_OPERATORS.includes(row.operator)) return { field: row.field, operator: row.operator }
    if (LIST_OPERATORS.includes(row.operator)) return { field: row.field, operator: row.operator, value: value.split(',').map((item) => item.trim()).filter(Boolean) }
    if (NUMERIC_OPERATORS.includes(row.operator)) return { field: row.field, operator: row.operator, value: Number(value) }
    return { field: row.field, operator: row.operator, value }
  })
  const save = useMutation({
    mutationFn: () => {
      const filters = { match, conditions: buildConditions() }
      return editingId ? contactsApi.updateSegment(editingId, { name: name.trim(), description: description.trim() || null, filters }, accessToken) : contactsApi.createSegment({ name: name.trim(), description: description.trim() || null, filters }, accessToken)
    },
    onSuccess: () => { setName(''); setDescription(''); setMatch('all'); setRows([{ field: 'email', operator: 'contains', value: '' }]); setEditingId(null); setError(''); void client.invalidateQueries({ queryKey: ['segments'] }) },
    onError: (cause) => setError(cause instanceof Error ? cause.message : 'Could not save segment'),
  })
  const remove = useMutation({ mutationFn: (id: string) => contactsApi.deleteSegment(id, accessToken), onSuccess: () => { setActiveId(null); setError(''); void client.invalidateQueries({ queryKey: ['segments'] }) }, onError: (cause) => setError(cause instanceof Error ? cause.message : 'Could not remove segment') })
  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (!name.trim()) { setError('Enter a segment name'); return }
    if (!rows.length) { setError('Add at least one condition'); return }
    const invalid = rows.some((row) => !EMPTY_OPERATORS.includes(row.operator) && !row.value.trim())
    if (invalid) { setError('Complete every condition that needs a value'); return }
    void save.mutate()
  }
  const edit = (segment: ContactSegment) => {
    setEditingId(segment.id); setName(segment.name); setDescription(segment.description ?? ''); setMatch(segment.filters.match)
    setRows(segment.filters.conditions.map((condition) => ({ field: condition.field, operator: condition.operator, value: Array.isArray(condition.value) ? condition.value.join(', ') : condition.value === null || condition.value === undefined ? '' : String(condition.value) })))
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }
  const updateRow = (index: number, patch: Partial<Row>) => setRows((current) => current.map((row, i) => i === index ? { ...row, ...patch } : row))
  return <main className="contact-form-page"><Link to="/contacts" className="back-link"><ArrowLeft size={16} /> Back to contacts</Link><div className="form-heading"><div><p className="eyebrow">CONTACT MANAGEMENT / AUDIENCES</p><h1>Segments</h1><p className="muted">Live audiences that update as your directory changes.</p></div></div><section className="management-card segment-builder"><div className="management-title"><ListFilter size={16} /><strong>{editingId ? 'Edit segment' : 'Define a segment'}</strong>{editingId && <span className="editing-badge">Editing</span>}</div><form className="custom-field-form" onSubmit={submit}><div className="form-grid"><label className="form-field"><span>Name</span><input value={name} onChange={(event) => { setName(event.target.value); setError('') }} placeholder="e.g. Prospects in SaaS" aria-label="Segment name" /></label><label className="form-field"><span>Match</span><select value={match} onChange={(event) => setMatch(event.target.value as 'all' | 'any')} aria-label="Match mode"><option value="all">All conditions (AND)</option><option value="any">Any condition (OR)</option></select></label></div><label className="form-field"><span>Description</span><input value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Optional context" aria-label="Segment description" /></label><div className="condition-heading"><strong>Conditions</strong><button className="small-button" type="button" onClick={() => setRows((current) => [...current, { field: 'email', operator: 'contains', value: '' }])}><Plus size={14} /> Add condition</button></div>{rows.map((row, index) => <div className="condition-row" key={index}><select value={row.field} onChange={(event) => updateRow(index, { field: event.target.value })} aria-label={`Condition ${index + 1} field`}>{fieldOptions.map((option) => <option value={option} key={option}>{option}</option>)}</select><select value={row.operator} onChange={(event) => updateRow(index, { operator: event.target.value as SegmentOperator })} aria-label={`Condition ${index + 1} operator`}>{OPERATORS.map((operator) => <option value={operator} key={operator}>{operator}</option>)}</select>{!EMPTY_OPERATORS.includes(row.operator) && <input value={row.value} onChange={(event) => updateRow(index, { value: event.target.value })} placeholder={LIST_OPERATORS.includes(row.operator) ? 'a, b, c' : 'Value'} aria-label={`Condition ${index + 1} value`} />}<button className="icon-button row-delete" type="button" aria-label="Remove condition" disabled={rows.length === 1} onClick={() => setRows((current) => current.filter((_, i) => i !== index))}><Trash2 size={15} /></button></div>)}<div className="condition-actions"><button className="small-button" type="submit" disabled={save.isPending}><Plus size={14} /> {save.isPending ? 'Saving...' : editingId ? 'Save changes' : 'Create segment'}</button>{editingId && <button className="toolbar-button clear-filter" type="button" onClick={() => { setEditingId(null); setName(''); setDescription(''); setMatch('all'); setRows([{ field: 'email', operator: 'contains', value: '' }]); setError('') }}>Cancel edit</button>}</div></form></section>{error && <div className="form-error" role="alert">{error}</div>}<div className="management-title tags-heading"><strong>Saved segments</strong><span>{segments.data?.length ?? 0}</span></div>{segments.isLoading ? <div className="table-state">Loading segments...</div> : segments.isError ? <div className="table-state error-state">{segments.error.message}</div> : (segments.data ?? []).length === 0 ? <div className="table-state"><strong>No segments yet</strong><span>Define your first audience above.</span></div> : <div className="management-list">{segments.data?.map((segment) => <div className={`audience-row list-row ${activeId === segment.id ? 'row-selected' : ''}`} key={segment.id}><div className="list-icon"><ListFilter size={17} /></div><div><strong>{segment.name}</strong><small className="muted">{segment.filters.match === 'all' ? 'Matches all' : 'Matches any'} · {segment.filters.conditions.length} condition{segment.filters.conditions.length === 1 ? '' : 's'}</small></div><button className="toolbar-button audience-toggle" type="button" onClick={() => setActiveId(activeId === segment.id ? null : segment.id)}><ChevronRight size={15} /> {activeId === segment.id ? 'Close' : 'Preview'}</button><button className="toolbar-button" type="button" onClick={() => edit(segment)}>Edit</button><button className="icon-button row-delete" type="button" aria-label="Delete segment" onClick={() => { if (window.confirm(`Delete segment "${segment.name}"?`)) void remove.mutate(segment.id) }}><Trash2 size={15} /></button></div>)}</div>}{activeId && <section className="management-card audience-members"><div className="management-title"><Play size={16} /><strong>Preview matches</strong><span>{members.data?.total ?? 0}</span></div>{members.isLoading ? <div className="table-state">Evaluating segment...</div> : members.isError ? <div className="table-state error-state">{members.error.message}</div> : (members.data?.items ?? []).length === 0 ? <div className="table-state"><strong>No matching contacts</strong><span>This segment currently matches no one.</span></div> : <div className="contact-table-wrap"><table className="contact-table"><thead><tr><th>Person</th><th>Company</th><th>Status</th></tr></thead><tbody>{members.data?.items.map((contact) => { const memberName = [contact.first_name, contact.last_name].filter(Boolean).join(' ') || contact.email; return <tr key={contact.id}><td><Link to={`/contacts/${contact.id}`} className="contact-person"><span className="contact-avatar">{(contact.first_name?.[0] ?? contact.email[0]).toUpperCase()}</span><span><strong>{memberName}</strong><small>{contact.email}</small></span></Link></td><td>{contact.company || <span className="faded">No company</span>}</td><td><span className={`status-badge ${contact.status.toLowerCase()}`}>{contact.status}</span></td></tr> })}</tbody></table></div>}</section>}</main>
}