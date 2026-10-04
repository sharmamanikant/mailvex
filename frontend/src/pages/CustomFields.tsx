import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Plus, Trash2 } from 'lucide-react'
import { contactsApi } from '../api/contacts'
import type { CustomFieldType } from '../types/contacts'

const FIELD_TYPES: CustomFieldType[] = ['TEXT', 'NUMBER', 'BOOLEAN', 'DATE', 'SELECT', 'MULTI_SELECT']

export default function CustomFields({ accessToken }: { accessToken: string }) {
  const client = useQueryClient()
  const [key, setKey] = useState('')
  const [label, setLabel] = useState('')
  const [fieldType, setFieldType] = useState<CustomFieldType>('TEXT')
  const [options, setOptions] = useState('')
  const [required, setRequired] = useState(false)
  const [error, setError] = useState('')
  const fields = useQuery({ queryKey: ['contact-fields'], queryFn: () => contactsApi.fieldDefinitions(accessToken) })
  const create = useMutation({ mutationFn: () => contactsApi.createFieldDefinition({ key: key.trim(), label: label.trim(), field_type: fieldType, options: options.split(',').map((item) => item.trim()).filter(Boolean), required }, accessToken), onSuccess: () => { setKey(''); setLabel(''); setOptions(''); setFieldType('TEXT'); setRequired(false); setError(''); void client.invalidateQueries({ queryKey: ['contact-fields'] }) }, onError: (cause) => setError(cause instanceof Error ? cause.message : 'Could not create field') })
  const remove = useMutation({ mutationFn: (id: string) => contactsApi.deleteFieldDefinition(id, accessToken), onSuccess: () => { setError(''); void client.invalidateQueries({ queryKey: ['contact-fields'] }) }, onError: (cause) => setError(cause instanceof Error ? cause.message : 'Could not remove field') })
  const needsOptions = fieldType === 'SELECT' || fieldType === 'MULTI_SELECT'
  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (!key.trim() || !label.trim()) { setError('Enter both a key and a label'); return }
    if (!/^[a-z][a-z0-9_]*$/.test(key.trim())) { setError('Keys must be lowercase letters, numbers, or underscores (e.g. years_at_company)'); return }
    if (needsOptions && !options.split(',').map((item) => item.trim()).some(Boolean)) { setError(`${fieldType} fields require at least one option`); return }
    void create.mutate()
  }
  return <main className="contact-form-page"><Link to="/contacts" className="back-link"><ArrowLeft size={16} /> Back to contacts</Link><div className="form-heading"><div><p className="eyebrow">CONTACT MANAGEMENT / SCHEMA</p><h1>Custom fields</h1><p className="muted">Define optional data points your team can capture on every recipient.</p></div></div><section className="management-card"><div className="management-title"><Plus size={16} /><strong>Define a custom field</strong></div><form className="custom-field-form" onSubmit={submit}><div className="form-grid"><label className="form-field"><span>Key</span><input value={key} onChange={(event) => { setKey(event.target.value); setError('') }} placeholder="skills_level" aria-label="Field key" /></label><label className="form-field"><span>Label</span><input value={label} onChange={(event) => { setLabel(event.target.value); setError('') }} placeholder="Skills level" aria-label="Field label" /></label><label className="form-field"><span>Type</span><select value={fieldType} onChange={(event) => { setFieldType(event.target.value as CustomFieldType); setError('') }} aria-label="Field type">{FIELD_TYPES.map((type) => <option value={type} key={type}>{type}</option>)}</select></label><label className="form-field"><span>Required</span><input type="checkbox" checked={required} onChange={(event) => setRequired(event.target.checked)} aria-label="Required" style={{ width: 18, height: 18, accentColor: 'var(--green)' }} /></label></div>{needsOptions && <label className="form-field options-field"><span>Options (comma separated)</span><input value={options} onChange={(event) => setOptions(event.target.value)} placeholder="Junior, Mid, Senior" aria-label="Field options" /></label>}<button className="small-button" type="submit" disabled={create.isPending}><Plus size={14} /> {create.isPending ? 'Creating...' : 'Add field'}</button></form></section>{error && <div className="form-error" role="alert">{error}</div>}<div className="management-title tags-heading"><strong>Defined fields</strong><span>{fields.data?.length ?? 0}</span></div>{fields.isLoading ? <div className="table-state">Loading fields...</div> : fields.isError ? <div className="table-state error-state">{fields.error.message}</div> : (fields.data ?? []).length === 0 ? <div className="table-state"><strong>No fields yet</strong><span>Define the first custom field above.</span></div> : <div className="contact-table-wrap"><table className="contact-table"><thead><tr><th>Field</th><th>Type</th><th>Options</th><th>Required</th><th aria-label="Actions" /></tr></thead><tbody>{fields.data?.map((field) => <tr key={field.id}><td><strong>{field.label}</strong><small className="muted field-key">{field.key}</small></td><td><span className="source-label">{field.field_type}</span></td><td>{field.options.length ? <div className="chip-row">{[...field.options].slice(0, 3).map((option) => <span className="data-chip" key={option}>{option}</span>)}{field.options.length > 3 && <span className="data-chip">+{field.options.length - 3}</span>}</div> : <span className="faded">None</span>}</td><td>{field.required ? 'Yes' : 'No'}</td><td className="row-actions"><button className="icon-button row-delete" type="button" aria-label="Delete field" onClick={() => { if (window.confirm(`Delete custom field "${field.label}"?`)) void remove.mutate(field.id) }}><Trash2 size={15} /></button></td></tr>)}</tbody></table></div>}</main>
}