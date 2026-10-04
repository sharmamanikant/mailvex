import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, ArrowLeft, CircleAlert, HeartPulse, Mail, Plug, RefreshCw, ShieldCheck } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'
import { workspaceSendersApi } from '../api/workspaceSenders'
import type { SenderHealthCheckResult, WorkspaceSenderDetail } from '../types/senders'

function statusTone(status: string): string {
  if (status === 'ACTIVE') return 'ok'
  if (status === 'ERROR' || status === 'REVOKED') return 'warn'
  if (status === 'REMOVED') return 'off'
  return 'neutral'
}

function statusLabel(status: string): string {
  return status.charAt(0) + status.slice(1).toLowerCase()
}

function healthTone(status: string): string {
  if (status === 'HEALTHY') return 'ok'
  if (status === 'WARNING' || status === 'CRITICAL') return 'warn'
  return 'off'
}

function healthLabel(status: string): string {
  if (status === 'CHECKING') return 'Checking'
  if (status === 'HEALTHY') return 'Healthy'
  if (status === 'WARNING') return 'Warning'
  if (status === 'CRITICAL') return 'Critical'
  return 'Unknown'
}

function checkTone(status: string): string {
  if (status === 'PASS') return 'ok'
  if (status === 'WARNING') return 'warn'
  if (status === 'FAIL') return 'crit'
  if (status === 'NOT_APPLICABLE') return 'neutral'
  return 'off'
}

function checkLabel(type: string): string {
  const labels: Record<string, string> = {
    PROVIDER_CONNECTION: 'Provider connection',
    MAILBOX_STATUS: 'Mailbox status',
    DOMAIN: 'Domain',
    SPF: 'SPF',
    DKIM: 'DKIM',
    DMARC: 'DMARC',
    DNS: 'DNS',
    SENDING_CONFIGURATION: 'Sending configuration',
    SENDING_SIGNALS: 'Sending signals',
  }
  return labels[type] ?? type.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function formatScore(value: number | null | undefined): string {
  return value == null ? '—' : `${Math.round(value)}`
}

function availabilityLabel(reason: string | null): string {
  const labels: Record<string, string> = {
    SENDER_DISABLED: 'Sending is disabled for this sender.',
    SENDER_REMOVED: 'This sender has been removed.',
    SENDER_ERROR: 'This sender is in an error state.',
    SENDER_REVOKED: 'This sender has been revoked.',
    PROVIDER_DISCONNECTED: 'The provider connection is disconnected.',
    PROVIDER_REVOKED: 'The provider connection has been revoked.',
    MAILBOX_SUSPENDED: 'The backing mailbox is suspended.',
    MAILBOX_DELETED: 'The backing mailbox is deleted.',
    MAILBOX_UNAVAILABLE: 'The backing mailbox is unavailable.',
  }
  return reason ? labels[reason] ?? reason : 'Availability cannot be determined.'
}

function formatDateTime(value: string | null | undefined): string | null {
  if (!value) return null
  if (Number.isNaN(Date.parse(value))) return null
  return new Date(value).toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function pill(name: string, tone: string) {
  return <span className={`status-pill status-${tone}`}><span />{name}</span>
}

export default function SenderDetail({ accessToken }: { accessToken: string }) {
  const { id } = useParams()
  const client = useQueryClient()
  const [confirmRemove, setConfirmRemove] = useState(false)
  const [notice, setNotice] = useState<{ kind: 'ok' | 'warn'; message: string } | null>(null)

  const detail = useQuery({
    queryKey: ['sender-detail', id],
    queryFn: () => workspaceSendersApi.getDetail(id as string, accessToken),
    enabled: Boolean(id),
  })

  const invalidate = () => {
    void client.invalidateQueries({ queryKey: ['sender-detail', id] })
    void client.invalidateQueries({ queryKey: ['senders'] })
  }

  const enable = useMutation({
    mutationFn: () => workspaceSendersApi.enable(id as string, accessToken),
    onSuccess: () => {
      invalidate()
      setNotice({ kind: 'ok', message: 'Sender enabled.' })
    },
    onError: (error: Error) => setNotice({ kind: 'warn', message: error.message || 'Enable failed.' }),
  })

  const disable = useMutation({
    mutationFn: () => workspaceSendersApi.disable(id as string, accessToken),
    onSuccess: () => {
      invalidate()
      setNotice({ kind: 'ok', message: 'Sender disabled.' })
    },
    onError: (error: Error) => setNotice({ kind: 'warn', message: error.message || 'Disable failed.' }),
  })

  const restore = useMutation({
    mutationFn: () => workspaceSendersApi.restore(id as string, accessToken),
    onSuccess: () => {
      invalidate()
      setNotice({ kind: 'ok', message: 'Sender restored. Re-enable it to start sending again.' })
    },
    onError: (error: Error) => setNotice({ kind: 'warn', message: error.message || 'Restore failed.' }),
  })

  const remove = useMutation({
    mutationFn: () => workspaceSendersApi.remove(id as string, accessToken),
    onSuccess: () => {
      invalidate()
      setConfirmRemove(false)
      setNotice({ kind: 'ok', message: 'Sender removed (soft delete). You can restore it.' })
    },
    onError: (error: Error) => setNotice({ kind: 'warn', message: error.message || 'Remove failed.' }),
  })

  const [showFindings, setShowFindings] = useState(false)
  const [showExplanation, setShowExplanation] = useState(false)

  const health = useQuery({
    queryKey: ['sender-health', id],
    queryFn: () => workspaceSendersApi.getHealth(id as string, accessToken),
    enabled: Boolean(id),
  })

  const healthHistory = useQuery({
    queryKey: ['sender-health-history', id],
    queryFn: () =>
      workspaceSendersApi.getHealthHistory(
        id as string,
        new URLSearchParams({ page: '1', page_size: '10' }),
        accessToken,
      ),
    enabled: Boolean(id),
  })

  const checkNow = useMutation({
    mutationFn: () => workspaceSendersApi.runHealthCheck(id as string, accessToken),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['sender-health', id] })
      void client.invalidateQueries({ queryKey: ['sender-health-history', id] })
      invalidate()
      setNotice({ kind: 'ok', message: 'Health check completed.' })
    },
    onError: (error: Error) => setNotice({ kind: 'warn', message: error.message || 'Health check failed.' }),
  })

  if (detail.isLoading) {
    return (
      <main className="dashboard-content">
        <Link className="back-link" to="/senders/workspace"><ArrowLeft size={16} /> Back to senders</Link>
        <div className="table-state"><ShieldCheck size={28} /><strong>Loading sender…</strong></div>
      </main>
    )
  }

  if (detail.isError || !detail.data) {
    return (
      <main className="dashboard-content">
        <Link className="back-link" to="/senders/workspace"><ArrowLeft size={16} /> Back to senders</Link>
        <div className="table-state error-state" role="alert">
          <CircleAlert size={28} />
          <strong>Sender not found</strong>
          <span>{detail.error instanceof Error ? detail.error.message : 'This sender may not exist in your workspace.'}</span>
        </div>
      </main>
    )
  }

  const sender: WorkspaceSenderDetail = detail.data
  const busy = enable.isPending || disable.isPending || restore.isPending || remove.isPending
  const removed = sender.status === 'REMOVED'
  const actionsBusy = busy || detail.isFetching

  const healthData = health.data
  const latest = healthData?.latest ?? null
  const findings = latest?.results ?? []

  return (
    <main className="dashboard-content">
      <Link className="back-link" to="/senders/workspace"><ArrowLeft size={16} /> Back to senders</Link>

      <div className="page-heading">
        <div>
          <p className="eyebrow">EMAIL SENDING</p>
          <h1>{sender.display_name ?? sender.email}</h1>
          <p className="muted">
            {sender.email} · {sender.provider}
          </p>
        </div>
        <div className="row-gap">
          <button className="button button-secondary" onClick={() => void detail.refetch()} disabled={actionsBusy} title="Refresh availability and state">
            <RefreshCw size={15} />
            Refresh
          </button>
          {removed ? (
            <button className="button" onClick={() => restore.mutate()} disabled={actionsBusy}>
              Restore sender
            </button>
          ) : (
            <>
              {sender.sending_enabled ? (
                <button className="button button-secondary" onClick={() => disable.mutate()} disabled={actionsBusy}>
                  Disable sending
                </button>
              ) : (
                <button className="button" onClick={() => enable.mutate()} disabled={actionsBusy}>
                  Enable sending
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {notice && (
        <div className={`notice notice-${notice.kind}`} role="status">
          <span>{notice.message}</span>
          <button className="icon-button" onClick={() => setNotice(null)} aria-label="Dismiss">
            <span aria-hidden="true">×</span>
          </button>
        </div>
      )}

      {!removed && !sender.availability.available && (
        <div className="notice notice-warn" role="status">
          <span>
            <strong>Not available for sending · </strong>
            {availabilityLabel(sender.availability.reason)}
          </span>
        </div>
      )}

      {removed && (
        <div className="notice notice-warn" role="status">
          <span><strong>Removed · </strong>This sender is inactive. Restore it to bring it back to the directory.</span>
        </div>
      )}

      <div className="detail-grid">
        <section className="detail-card">
          <h2 className="row-gap"><ShieldCheck size={15} /> Overview</h2>
          <div className="detail-row"><span>Status</span><strong>{pill(statusLabel(sender.status), statusTone(sender.status))}</strong></div>
          <div className="detail-row"><span>Sending</span><strong>{pill(sender.sending_enabled ? 'Enabled' : 'Disabled', sender.sending_enabled ? 'ok' : 'off')}</strong></div>
          <div className="detail-row"><span>Provider</span><strong>{sender.provider}</strong></div>
          <div className="detail-row"><span>Availability</span><strong>{pill(sender.availability.available ? 'Available' : 'Unavailable', sender.availability.available ? 'ok' : 'warn')}</strong></div>
          <div className="detail-row"><span>Created</span><strong>{formatDateTime(sender.created_at) ?? '—'}</strong></div>
          <div className="detail-row"><span>Updated</span><strong>{formatDateTime(sender.updated_at) ?? '—'}</strong></div>
        </section>

        <section className="detail-card">
          <h2 className="row-gap"><Mail size={15} /> Mailbox</h2>
          {sender.mailbox ? (
            <>
              <div className="detail-row"><span>Email</span><strong>{sender.mailbox.email}</strong></div>
              <div className="detail-row"><span>Name</span><strong>{sender.mailbox.display_name ?? '—'}</strong></div>
              <div className="detail-row"><span>Department</span><strong>{sender.mailbox.department ?? '—'}</strong></div>
              <div className="detail-row"><span>Job title</span><strong>{sender.mailbox.job_title ?? '—'}</strong></div>
              <div className="detail-row"><span>Status</span><strong>{pill(statusLabel(sender.mailbox.status), sender.mailbox.is_deleted ? 'off' : sender.mailbox.is_suspended ? 'warn' : 'ok')}</strong></div>
              <div className="detail-row"><span>Discovered</span><strong>{formatDateTime(sender.mailbox.last_discovered_at) ?? '—'}</strong></div>
            </>
          ) : (
            <p className="muted">No mailbox associated with this sender.</p>
          )}
        </section>

        <section className="detail-card">
          <h2 className="row-gap"><Plug size={15} /> Provider connection</h2>
          {sender.provider_connection ? (
            <>
              <div className="detail-row"><span>Provider</span><strong>{sender.provider_connection.provider}</strong></div>
              <div className="detail-row"><span>Status</span><strong>{pill(statusLabel(sender.provider_connection.status), sender.provider_connection.status === 'CONNECTED' ? 'ok' : sender.provider_connection.status === 'REVOKED' ? 'warn' : 'off')}</strong></div>
              <div className="detail-row"><span>Workspace</span><strong>{sender.provider_connection.workspace_domain ?? '—'}</strong></div>
              <div className="detail-row"><span>Type</span><strong>{sender.provider_connection.connection_type ?? '—'}</strong></div>
              <div className="detail-row"><span>Last sync</span><strong>{sender.provider_connection.last_sync_status ?? '—'}</strong></div>
              <div className="detail-row"><span>Synced at</span><strong>{formatDateTime(sender.provider_connection.last_sync_completed_at) ?? '—'}</strong></div>
            </>
          ) : (
            <p className="muted">No provider connection associated with this sender.</p>
          )}
        </section>

        <section className="detail-card">
          <h2 className="row-gap"><HeartPulse size={15} /> Health</h2>
          <div className="detail-row"><span>Status</span><strong>{pill(healthLabel(healthData?.health_status ?? 'UNKNOWN'), healthTone(healthData?.health_status ?? 'UNKNOWN'))}</strong></div>
          <div className="detail-row"><span>Score</span><strong className="health-score">{formatScore(healthData?.health_score ?? sender.health?.score)}</strong></div>
          <div className="detail-row"><span>Last check</span><strong>{formatDateTime(healthData?.last_health_check_at ?? sender.health?.last_checked_at) ?? '—'}</strong></div>

          {healthData?.summary && <p className="muted small" style={{ marginTop: 13 }}>{healthData.summary}</p>}

          {!removed && (
            <div className="row-gap" style={{ marginTop: 15 }}>
              <button className="button" onClick={() => checkNow.mutate()} disabled={checkNow.isPending || actionsBusy}>
                <RefreshCw size={14} />
                {checkNow.isPending ? 'Checking…' : 'Check health'}
              </button>
              {findings.length > 0 && (
                <button className="button button-secondary" onClick={() => setShowFindings((value) => !value)}>
                  {showFindings ? 'Hide findings' : 'View findings'}
                </button>
              )}
            </div>
          )}

          {latest && (
            <p className="muted small" style={{ marginTop: 13 }}>
              Version {latest.score_version} · {latest.triggered_by.toLowerCase()} · {formatDateTime(latest.completed_at ?? latest.started_at) ?? '—'}
            </p>
          )}

          {showFindings && findings.length > 0 && (
            <div className="health-findings" style={{ marginTop: 8 }}>
              {findings.map((result: SenderHealthCheckResult) => (
                <div className="health-check" key={result.check_type}>
                  <div className="row-gap">
                    <span className={`status-pill status-${checkTone(result.status)}`}><span />{result.status}</span>
                    <strong className="small">{checkLabel(result.check_type)}</strong>
                    {result.score != null && <span className="health-score" style={{ marginLeft: 'auto' }}>{Math.round(result.score)} / 100</span>}
                  </div>
                  <p className="muted small" style={{ margin: 0 }}>
                    {result.summary ?? result.technical_details ?? 'No additional findings.'}
                  </p>
                  {result.recommendation && (
                    <p className="small health-recommendation">
                      <strong>Recommendation:</strong> {result.recommendation}
                    </p>
                  )}
                </div>
              ))}
            </div>
          )}

          {latest?.score_explanation && (
            <button
              className="text-button"
              style={{ marginTop: 14 }}
              onClick={() => setShowExplanation((value) => !value)}
            >
              {showExplanation ? 'Hide how this is calculated' : 'How is this score calculated?'}
            </button>
          )}

          {showExplanation && latest?.score_explanation && (
            <div className="health-explanation" style={{ marginTop: 10 }}>
              {Object.entries(latest.score_explanation.weights).map(([type, weight]) => (
                <div className="row-gap" key={type}>
                  <span className="muted small" style={{ flex: 1 }}>{checkLabel(type)}</span>
                  <span className="small">{weight}%</span>
                </div>
              ))}
              <p className="muted small" style={{ margin: '9px 0 0' }}>
                {latest.score_explanation.unknown_handling}
              </p>
              <p className="small" style={{ margin: 0 }}>
                Version {latest.score_explanation.version} · Healthy ≥ {latest.score_explanation.thresholds['healthy']} · Warning ≥ {latest.score_explanation.thresholds['warning']}
              </p>
            </div>
          )}
        </section>
      </div>

      {!removed && (
        <section className="detail-card" style={{ marginTop: 13 }}>
          <h2 className="row-gap"><Activity size={15} /> Health history</h2>
          {healthHistory.isLoading ? (
            <div className="table-state"><strong>Loading history…</strong></div>
          ) : (healthHistory.data?.items.length ?? 0) > 0 ? (
            <div className="table-wrap" style={{ marginTop: 12 }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Result</th>
                    <th>Score</th>
                    <th>Trigger</th>
                    <th>Checks</th>
                  </tr>
                </thead>
                <tbody>
                  {(healthHistory.data?.items ?? []).map((item) => (
                    <tr key={item.health_check_id}>
                      <td className="muted small">{formatDateTime(item.started_at) ?? '—'}</td>
                      <td>
                        <span className={`status-pill status-${healthTone(item.overall_status)}`}><span />{healthLabel(item.overall_status)}</span>
                      </td>
                      <td className="small">{formatScore(item.overall_score)}</td>
                      <td className="small">{item.triggered_by.toLowerCase()}</td>
                      <td className="small">{item.result_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="muted small">No health checks recorded yet. Run a check to get started.</p>
          )}
        </section>
      )}

      {!removed && (
        <div className="row-gap" style={{ marginTop: 18 }}>
          {!confirmRemove ? (
            <button className="button button-danger" onClick={() => setConfirmRemove(true)} disabled={busy}>
              Remove sender
            </button>
          ) : (
            <>
              <span className="muted small">Remove this sender? The record is soft-deleted and can be restored later.</span>
              <button className="button button-danger" onClick={() => remove.mutate()} disabled={busy}>
                Confirm remove
              </button>
              <button className="button button-secondary" onClick={() => setConfirmRemove(false)} disabled={busy}>
                Cancel
              </button>
            </>
          )}
        </div>
      )}
    </main>
  )
}