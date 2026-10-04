import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ArrowLeft, Ban, CheckCircle2, Download, FileSpreadsheet, Loader2, Play, RotateCcw, Upload, XCircle } from 'lucide-react'
import { importsApi, type DuplicatePolicy, type ImportJob } from '../api/imports'
import { contactsApi } from '../api/contacts'

const ACTIVE_STATUSES = ['UPLOADED', 'VALIDATING', 'IMPORTING']
const TERMINAL_STATUSES = ['COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELLED']
const WIZARD_STEPS = ['Upload', 'Map columns', 'Preview', 'Validate', 'Import', 'Results']

const STANDARD_FIELD_LABELS: Record<string, string> = {
  full_name: 'Full name',
  first_name: 'First name',
  last_name: 'Last name',
  email: 'Email',
  phone: 'Phone',
  company: 'Company',
  designation: 'Designation',
  location: 'Location',
  website: 'Website',
  industry: 'Industry',
  source: 'Source',
  source_reference: 'Source reference',
  notes: 'Notes',
}

function stepIndexFor(status: string | undefined): number {
  switch (status) {
    case 'UPLOADED':
      return 0
    case 'VALIDATING':
      return 1
    case 'READY':
      return 1
    case 'IMPORTING':
      return 3
    case 'COMPLETED':
    case 'COMPLETED_WITH_ERRORS':
    case 'FAILED':
    case 'CANCELLED':
      return 5
    default:
      return 0
  }
}

export default function ContactImport({ accessToken }: { accessToken: string }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const urlJobId = searchParams.get('job')
  const [job, setJob] = useState<ImportJob | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [policy, setPolicy] = useState<DuplicatePolicy>('SKIP')
  const lastJobId = useRef<string | null>(null)

  const jobId = job?.id ?? urlJobId
  const jobQuery = useQuery({
    queryKey: ['import-job', jobId],
    queryFn: () => importsApi.get(jobId as string, accessToken),
    enabled: Boolean(jobId) && Boolean(accessToken),
    refetchInterval: (query) => (ACTIVE_STATUSES.includes(String(query.state.data?.status ?? '')) ? 1500 : false),
  })
  useEffect(() => {
    if (jobQuery.data) setJob(jobQuery.data)
  }, [jobQuery.data])

  const fields = useQuery({
    queryKey: ['contact-fields', accessToken ? 'auth' : 'none'],
    queryFn: () => contactsApi.fieldDefinitions(accessToken),
    enabled: Boolean(accessToken),
  })
  const fieldLabels: Record<string, string> = {
    ...STANDARD_FIELD_LABELS,
    ...Object.fromEntries((fields.data ?? []).map((field) => [field.key, field.label])),
  }
  const targetFields = Object.keys(fieldLabels)
  const columnToField = (() => {
    const map: Record<string, string> = {}
    for (const [field, column] of Object.entries(draft)) {
      if (!map[column]) map[column] = field
    }
    return map
  })()

  useEffect(() => {
    if (!job) return
    if (job.id !== lastJobId.current) {
      lastJobId.current = job.id
      setDraft(job.column_mapping ?? {})
      setPolicy(job.duplicate_policy ?? 'SKIP')
      setError('')
    }
  }, [job])

  useEffect(() => {
    if (!jobId) lastJobId.current = null
  }, [jobId])

  const chooseFile = async (selected: File | null) => {
    setError('')
    if (!selected) return
    setBusy(true)
    try {
      const created = await importsApi.create(selected, accessToken)
      setJob(created)
      setSearchParams({ job: created.id }, { replace: true })
      lastJobId.current = null
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not upload file')
    } finally {
      setBusy(false)
    }
  }

  const assignColumn = (column: string, field: string) => {
    const next = { ...draft }
    for (const [existingField, existingColumn] of Object.entries(next)) {
      if (existingField === field || existingColumn === column) delete next[existingField]
    }
    if (field) next[field] = column
    setDraft(next)
  }

  const saveAndStart = async () => {
    if (!job) return
    if (!draft.email) {
      setError('Please map at least one column to Email before starting the import.')
      return
    }
    setBusy(true)
    setError('')
    try {
      const updated = await importsApi.update(job.id, { column_mapping: draft, duplicate_policy: policy }, accessToken)
      setJob(await importsApi.start(updated.id, accessToken))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not start import')
      setBusy(false)
    }
  }

  const saveMapping = async () => {
    if (!job) return
    setBusy(true)
    setError('')
    try {
      const updated = await importsApi.update(job.id, { column_mapping: draft, duplicate_policy: policy }, accessToken)
      setJob(updated)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not save mapping')
    } finally {
      setBusy(false)
    }
  }

  const cancelJob = async () => {
    if (!job) return
    setError('')
    try {
      setJob(await importsApi.cancel(job.id, accessToken))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not cancel import')
    }
  }

  const retryJob = async () => {
    if (!job) return
    setError('')
    try {
      setJob(await importsApi.retry(job.id, accessToken))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not retry import')
    }
  }

  const resetToUpload = () => {
    setJob(null)
    setError('')
    setDraft({})
    setSearchParams({}, { replace: true })
  }

  const status = job?.status ?? ''
  const activeStep = stepIndexFor(status)
  const running = ACTIVE_STATUSES.includes(status)
  const terminal = TERMINAL_STATUSES.includes(status)
  const progress = job && job.total_rows > 0 ? Math.min(100, Math.round((job.processed_rows / job.total_rows) * 100)) : 0
  const headers = job?.preview?.headers ?? []
  const previewRows = job?.preview?.rows ?? []

  return (
    <main className="import-page">
      <Link to="/contacts" className="back-link">
        <ArrowLeft size={16} /> Back to contacts
      </Link>
      <div className="import-heading">
        <div>
          <p className="eyebrow">RECIPIENT DATA</p>
          <h1>Import contacts</h1>
          <p className="muted">Upload a clean CSV or XLSX file, map its columns, then run the import in the background.</p>
        </div>
      </div>

      {WIZARD_STEPS.length > 0 && (
        <ol className="import-steps">
          {WIZARD_STEPS.map((label, index) => (
            <li key={label} className={index < activeStep ? 'done' : index === activeStep ? 'active' : index <= activeStep && running && index >= 3 ? 'active' : ''}>
              <span className="step-number">{index < activeStep || (index === activeStep && terminal) ? <CheckCircle2 size={14} /> : index + 1}</span>
              {label}
            </li>
          ))}
        </ol>
      )}

      {error && <div className="form-error">{error}</div>}

      {!jobId && (
        <div className="panel">
          <label className="upload-zone">
            <input type="file" accept=".csv,.xlsx" onChange={(event) => void chooseFile(event.target.files?.[0] ?? null)} />
            <FileSpreadsheet size={30} />
            <strong>{busy ? 'Uploading file...' : 'Choose a CSV or XLSX file'}</strong>
            <span>Up to 10 MB. The first row is used as column headers. Only .csv and .xlsx are accepted.</span>
          </label>
        </div>
      )}

      {jobId && !terminal && status === 'UPLOADED' && (
        <div className="center-box">
          <Loader2 size={26} className="spin" />
          <p>Uploaded. Waiting for the file to be validated...</p>
        </div>
      )}

      {jobId && !terminal && status === 'VALIDATING' && (
        <div className="center-box">
          <Loader2 size={26} className="spin" />
          <p>Validating file structure and detecting columns...</p>
        </div>
      )}

      {jobId && terminal && status === 'FAILED' && !error && (
        <div className="result-panel error">
          <XCircle size={26} />
          <h2>File validation failed</h2>
          <p className="muted">{job?.error_message ?? 'The uploaded file could not be processed.'}</p>
          <div className="form-actions">
            <button className="outline-button" type="button" onClick={resetToUpload}>
              <Upload size={15} /> Choose another file
            </button>
          </div>
        </div>
      )}

      {status === 'READY' && job && (
        <>
          <section className="panel">
            <div className="preview-top">
              <div>
                <p className="eyebrow">STEP 2 OF 6</p>
                <h2>Map columns to fields</h2>
                <p className="muted">{job.filename} · {job.preview?.row_count ?? 0} rows detected</p>
              </div>
              <button className="outline-button" type="button" onClick={resetToUpload}>
                <RotateCcw size={15} /> Choose another
              </button>
            </div>
            <div className="mapping-hint">Choose which column each field should come from. Ignore columns you don’t need.</div>
            <div className="mapping-grid">
              {headers.map((header) => (
                <div className="mapping-line" key={header}>
                  <span className="mapping-column">{header}</span>
                  <select
                    value={columnToField[header] ?? ''}
                    onChange={(event) => assignColumn(header, event.target.value)}
                    aria-label={`Map column ${header}`}
                  >
                    <option value="">— Ignore —</option>
                    {targetFields.map((field) => (
                      <option key={field} value={field}>
                        {fieldLabels[field]}
                      </option>
                    ))}
                  </select>
                </div>
              ))}
            </div>
            <div className="policy-row">
              <strong>Duplicate contacts</strong>
              <label>
                <input type="radio" name="policy" checked={policy === 'SKIP'} onChange={() => setPolicy('SKIP')} />
                Skip existing
              </label>
              <label>
                <input type="radio" name="policy" checked={policy === 'UPDATE'} onChange={() => setPolicy('UPDATE')} />
                Update details
              </label>
              <label>
                <input type="radio" name="policy" checked={policy === 'CREATE_NEW'} onChange={() => setPolicy('CREATE_NEW')} />
                Create new
              </label>
            </div>
            <div className="form-actions">
              <button className="outline-button compact" type="button" disabled={busy} onClick={() => void saveMapping()}>
                Save mapping
              </button>
              <button className="primary-button compact" type="button" disabled={busy || !draft.email} onClick={() => void saveAndStart()}>
                <Play size={15} /> {busy ? 'Starting...' : 'Validate & start import'}
              </button>
            </div>
            {!draft.email && <div className="form-warn">Map a column to Email before starting the import.</div>}
          </section>

          {job.preview && (
            <section className="panel">
              <div className="preview-top">
                <div>
                  <p className="eyebrow">STEP 3 OF 6</p>
                  <h2>Preview</h2>
                  <p className="muted">Showing the first {previewRows.length} rows exactly as they will be read.</p>
                </div>
              </div>
              <div className="mapping-row">
                {Object.entries(draft).map(([field, column]) => (
                  <span className="mapping-chip" key={field}>
                    {field}: {column}
                  </span>
                ))}
              </div>
              {headers.length > 0 && (
                <div className="preview-table">
                  <table>
                    <thead>
                      <tr>
                        {headers.map((header) => (
                          <th key={header}>{header}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {previewRows.map((row, index) => (
                        <tr key={index}>
                          {headers.map((header) => (
                            <td key={header}>{row[header]}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          )}
        </>
      )}

      {running && status === 'IMPORTING' && job && (
        <section className="panel">
          <div className="preview-top">
            <div>
              <p className="eyebrow">STEPS 4–5 OF 6</p>
              <h2>Validating and importing</h2>
              <p className="muted">This import runs in the background. You can leave this page and return later.</p>
            </div>
            <button className="outline-button" type="button" onClick={() => void cancelJob()}>
              <Ban size={15} /> Cancel import
            </button>
          </div>
          <div className="progress-track">
            <div className="progress-fill" style={{ width: `${progress}%` }} />
          </div>
          <div className="progress-meta">
            <strong>{progress}%</strong>
            <span>
              {job.processed_rows} of {job.total_rows} rows processed
            </span>
          </div>
          <div className="stat-grid">
            <Stat label="Imported" value={job.successful_rows - job.updated_rows} />
            <Stat label="Updated" value={job.updated_rows} />
            <Stat label="Duplicates" value={job.duplicate_rows} />
            <Stat label="Suppressed" value={job.suppressed_rows} />
            <Stat label="Invalid" value={job.failed_rows} />
          </div>
        </section>
      )}

      {terminal && status !== 'FAILED' && job && (
        <section className="panel">
          <div className="result-icon">{status === 'COMPLETED' ? <CheckCircle2 size={28} /> : status === 'COMPLETED_WITH_ERRORS' ? <AlertTriangle size={28} /> : <Ban size={28} />}</div>
          <h2>{status === 'COMPLETED' ? 'Import complete' : status === 'COMPLETED_WITH_ERRORS' ? 'Import finished with issues' : 'Import cancelled'}</h2>
          {status === 'CANCELLED' && <p className="muted">The import was cancelled before it finished. No further rows will be processed.</p>}
          <div className="stat-grid">
            <Stat label="Processed" value={job.processed_rows} />
            <Stat label="Imported" value={job.successful_rows - job.updated_rows} />
            <Stat label="Updated" value={job.updated_rows} />
            <Stat label="Duplicates" value={job.duplicate_rows} />
            <Stat label="Suppressed" value={job.suppressed_rows} />
            <Stat label="Invalid" value={job.failed_rows} />
          </div>
          {status === 'COMPLETED_WITH_ERRORS' && job.error_report_available && (
            <a className="outline-button inline" href={importsApi.errorReportUrl(job.id)} download>
              <Download size={15} /> Download error report
            </a>
          )}
          {status === 'CANCELLED' && (
            <button className="primary-button compact" type="button" onClick={() => void retryJob()}>
              <RotateCcw size={15} /> Retry import
            </button>
          )}
          <div className="form-actions">
            <Link className="primary-button compact" to="/contacts">
              View contacts
            </Link>
            <button className="outline-button compact" type="button" onClick={resetToUpload}>
              <Upload size={15} /> Import another file
            </button>
          </div>
        </section>
      )}
    </main>
  )
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="stat-tile">
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  )
}