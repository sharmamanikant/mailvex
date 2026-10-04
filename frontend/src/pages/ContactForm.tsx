import { zodResolver } from '@hookform/resolvers/zod'
import { useEffect, useState } from 'react'
import { useForm, type UseFormRegister } from 'react-hook-form'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { z } from 'zod'
import { ArrowLeft, Save } from 'lucide-react'
import { contactsApi } from '../api/contacts'
import type { ContactFieldDefinition, ContactInput } from '../types/contacts'

const schema = z.object({ first_name: z.string().max(100).optional(), last_name: z.string().max(100).optional(), email: z.string().email('Enter a valid email'), phone: z.string().max(50).optional(), company: z.string().max(200).optional(), designation: z.string().max(200).optional(), location: z.string().max(200).optional(), website: z.string().max(500).optional(), industry: z.string().max(150).optional(), source: z.string().max(100).optional(), source_reference: z.string().max(500).optional(), status: z.string(), custom_fields: z.record(z.string(), z.string()), tag_ids: z.array(z.string()).optional(), list_ids: z.array(z.string()).optional() })
type Values = z.infer<typeof schema>
const blank: Values = { first_name: '', last_name: '', email: '', phone: '', company: '', designation: '', location: '', website: '', industry: '', source: 'manual', source_reference: '', status: 'ACTIVE', custom_fields: {}, tag_ids: [], list_ids: [] }

export default function ContactForm({ accessToken }: { accessToken: string }) {
  const { id } = useParams()
  const navigate = useNavigate()
  const client = useQueryClient()
  const editing = Boolean(id)
  const [formError, setFormError] = useState('')
  const existing = useQuery({ queryKey: ['contact', id], queryFn: () => contactsApi.get(id!, accessToken), enabled: editing })
  const fields = useQuery({ queryKey: ['contact-fields'], queryFn: () => contactsApi.fieldDefinitions(accessToken) })
  const lists = useQuery({ queryKey: ['contact-lists'], queryFn: () => contactsApi.lists(accessToken) })
  const tags = useQuery({ queryKey: ['contact-tags'], queryFn: () => contactsApi.tags(accessToken) })
  const { register, handleSubmit, reset, watch, setValue, formState: { errors, isSubmitting } } = useForm<Values>({ resolver: zodResolver(schema), defaultValues: blank })
  useEffect(() => {
    if (existing.data) {
      const contact = existing.data
      const tagIds = (tags.data ?? []).filter((tag) => contact.tags.includes(tag.name)).map((tag) => tag.id)
      reset({ first_name: contact.first_name ?? '', last_name: contact.last_name ?? '', email: contact.email, phone: contact.phone ?? '', company: contact.company ?? '', designation: contact.designation ?? '', location: contact.location ?? '', website: contact.website ?? '', industry: contact.industry ?? '', source: contact.source ?? '', source_reference: contact.source_reference ?? '', status: contact.status, custom_fields: contact.custom_fields, tag_ids: tagIds, list_ids: contact.list_ids })
    }
  }, [existing.data, reset])
  const save = useMutation({
    mutationFn: (values: Values) => {
      const input = { ...values, custom_fields: Object.fromEntries(Object.entries(values.custom_fields).filter(([, value]) => value && value.trim())) } as ContactInput
      return editing ? contactsApi.update(id!, input, accessToken) : contactsApi.create(input, accessToken)
    },
    onSuccess: (contact) => { void client.invalidateQueries({ queryKey: ['contacts'] }); navigate(`/contacts/${contact.id}`) },
  })
  const submit = (values: Values) => {
    const missing = (fields.data ?? []).filter((field) => field.required && !String(values.custom_fields[field.key] ?? '').trim())
    if (missing.length) { setFormError(`Complete required custom fields: ${missing.map((field) => field.label).join(', ')}`); return }
    save.mutate(values)
  }
  const customFields = watch('custom_fields') ?? {}
  return <main className="contact-form-page"><Link to="/contacts" className="back-link"><ArrowLeft size={16} /> Back to contacts</Link><div className="form-heading"><div><p className="eyebrow">{editing ? 'EDIT RECIPIENT' : 'NEW RECIPIENT'}</p><h1>{editing ? 'Update contact' : 'Add a contact'}</h1><p className="muted">Keep recipient context accurate and useful.</p></div></div>{existing.isLoading ? <div className="table-state">Loading contact...</div> : <form className="contact-form" onSubmit={handleSubmit(submit)}><section className="form-section"><div className="section-heading"><strong>Identity</strong><span>How this person should appear in your workspace.</span></div><div className="form-grid"><Field label="First name" error={errors.first_name?.message}><input {...register('first_name')} /></Field><Field label="Last name" error={errors.last_name?.message}><input {...register('last_name')} /></Field><Field label="Email" error={errors.email?.message} required><input type="email" {...register('email')} /></Field><Field label="Phone"><input {...register('phone')} /></Field></div></section><section className="form-section"><div className="section-heading"><strong>Professional context</strong><span>Useful context for personalization, without guessing.</span></div><div className="form-grid"><Field label="Company"><input {...register('company')} /></Field><Field label="Designation"><input {...register('designation')} /></Field><Field label="Location"><input {...register('location')} /></Field><Field label="Industry"><input {...register('industry')} /></Field><Field label="Website"><input {...register('website')} /></Field><Field label="Source"><input {...register('source')} /></Field><Field label="Source reference"><input {...register('source_reference')} /></Field><Field label="Status"><select {...register('status')}><option value="ACTIVE">Active</option><option value="INACTIVE">Inactive</option></select></Field></div></section><section className="form-section"><div className="section-heading"><strong>Audience</strong><span>Attach this contact to groups and signals.</span></div><div className="form-grid"><Field label="Tags"><select {...register('tag_ids')} multiple size={Math.max(2, Math.min(6, (tags.data ?? []).length))}>{tags.data?.map((tag) => <option value={tag.id} key={tag.id}>{tag.name}</option>)}</select></Field><Field label="Lists"><select {...register('list_ids')} multiple size={Math.max(2, Math.min(6, (lists.data ?? []).length))}>{lists.data?.map((list) => <option value={list.id} key={list.id}>{list.name}</option>)}</select></Field></div></section><section className="form-section"><div className="section-heading"><strong>Custom fields</strong><span>Optional recipient details defined in your workspace.</span></div>{fields.isLoading ? <div className="table-state">Loading custom fields...</div> : (fields.data ?? []).length === 0 ? <p className="muted">No custom fields are defined yet. Add them from the Custom Fields page.</p> : <div className="form-grid">{(fields.data ?? []).map((field) => <CustomFieldField key={field.key} field={field} value={customFields[field.key] ?? ''} onMulti={(option, checked) => { const options = new Set((customFields[field.key] ?? '').split(',').filter(Boolean)); if (checked) options.add(option); else options.delete(option); setValue(`custom_fields.${field.key}` as any, [...options].join(',')) }} register={register} />)}</div>}</section>{save.isError && <div className="form-error">{save.error.message}</div>}{formError && <div className="form-error" role="alert">{formError}</div>}<div className="form-actions"><Link to="/contacts" className="outline-button">Cancel</Link><button className="primary-button compact" type="submit" disabled={isSubmitting || save.isPending}><Save size={16} /> {save.isPending ? 'Saving...' : editing ? 'Save changes' : 'Create contact'}</button></div></form>}</main>
}
function Field({ label, error, required, children }: { label: string; error?: string; required?: boolean; children: React.ReactNode }) { return <label className="form-field"><span>{label}{required && <b> *</b>}</span>{children}{error && <small className="field-error">{error}</small>}</label> }
function CustomFieldField({ field, value, onMulti, register }: { field: ContactFieldDefinition; value: string; onMulti: (option: string, checked: boolean) => void; register: UseFormRegister<Values> }) {
  const key = field.key
  if (field.field_type === 'SELECT') return <Field label={field.label} required={field.required}><select {...register(`custom_fields.${key}`)}><option value="">{field.required ? 'Select...' : 'None'}</option>{field.options.map((option) => <option value={option} key={option}>{option}</option>)}</select></Field>
  if (field.field_type === 'MULTI_SELECT') return <Field label={field.label} required={field.required}><div className="chip-row">{field.options.map((option) => { const active = value.split(',').includes(option); return <label className="data-chip" style={{ cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 6 }} key={option}><input type="checkbox" checked={active} onChange={(event) => onMulti(option, event.target.checked)} style={{ accentColor: 'var(--green)', width: 13, height: 13, margin: 0 }} /><span>{option}</span></label> })}</div></Field>
  if (field.field_type === 'BOOLEAN') return <Field label={field.label} required={field.required}><select {...register(`custom_fields.${key}`)}><option value="">{field.required ? 'Select...' : 'None'}</option><option value="true">Yes</option><option value="false">No</option></select></Field>
  return <Field label={field.label} required={field.required}><input type={field.field_type === 'NUMBER' ? 'number' : field.field_type === 'DATE' ? 'date' : 'text'} {...register(`custom_fields.${key}`)} /></Field>
}