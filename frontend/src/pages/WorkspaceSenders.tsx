import { useState } from 'react'
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, Mail, Search, ShieldCheck } from 'lucide-react'
import { Link } from 'react-router-dom'
import { workspaceSendersApi } from '../api/workspaceSenders'
import type { WorkspaceSender } from '../types/senders'

const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: '', label: 'All statuses' },
  { value: 'ACTIVE', label: 'Active' },
  { value: 'DISABLED', label: 'Disabled' },
  { value: 'ERROR', label: 'Error' },
  { value: 'REVOKED', label: 'Revoked' },
  { value: 'REMOVED', label: 'Removed' },
]

const PROVIDER_FILTERS: { value: string; label: string }[] = [
  { value: '', label: 'All providers' },
  { value: 'GOOGLE', label: 'Google' },
  { value: 'MICROSOFT', label: 'Microsoft' },
  { value: 'SMTP', label: 'SMTP' },
  { value: 'FUTURE_ESP', label: 'Future ESP' },
]

const HEALTH_FILTERS: { value: string; label: string }[] = [
  { value: '', label: 'All health' },
  { value: 'HEALTHY', label: 'Healthy' },
  { value: 'WARNING', label: 'Warning' },
  { value: 'CRITICAL', label: 'Critical' },
  { value: 'CHECKING', label: 'Checking' },
  { value: 'UNKNOWN', label: 'Unknown' },
]

const SENDING_FILTERS: { value: string; label: string }[] = [
  { value: '', label: 'Sending: all' },
  { value: 'true', label: 'Sending: enabled' },
  { value: 'false', label: 'Sending: disabled' },
]

const SORT_OPTIONS: { value: string; label: string }[] = [
  { value: 'email', label: 'Sort: Email A–Z' },
  { value: '-email', label: 'Sort: Email Z–A' },
  { value: '-created_at', label: 'Sort: Newest' },
  { value: '-updated_at', label: 'Sort: Recently updated' },
  { value: 'status', label: 'Sort: Status' },
  { value: '-last_health_check_at', label: 'Sort: Last checked' },
]

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

function availabilityLabel(reason: string | null): string {
  const labels: Record<string, string> = {
    SENDER_DISABLED: 'Sending disabled',
    SENDER_REMOVED: 'Removed',
    SENDER_ERROR: 'Sender error',
    SENDER_REVOKED: 'Sender revoked',
    PROVIDER_DISCONNECTED: 'Provider disconnected',
    PROVIDER_REVOKED: 'Provider revoked',
    MAILBOX_SUSPENDED: 'Mailbox suspended',
    MAILBOX_DELETED: 'Mailbox deleted',
    MAILBOX_UNAVAILABLE: 'Mailbox unavailable',
  }
  return reason ? labels[reason] ?? reason : 'Unknown'
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

export default function WorkspaceSenders({ accessToken }: { accessToken: string }) {
  const client = useQueryClient()
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [providerFilter, setProviderFilter] = useState('')
  const [healthFilter, setHealthFilter] = useState('')
  const [sendingFilter, setSendingFilter] = useState('')
  const [sort, setSort] = useState('-created_at')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const [notice, setNotice] = useState<{ kind: 'ok' | 'warn'; message: string } | null>(null)

  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
    sort,
  })
  if (search.trim()) params.set('search', search.trim())
  if (statusFilter) params.set('status', statusFilter)
  if (providerFilter) params.set('provider', providerFilter)
  if (healthFilter) params.set('health_status', healthFilter)
  if (sendingFilter) params.set('sending', sendingFilter)

  const senders = useQuery({
    queryKey: ['senders', params.toString()],
    queryFn: () => workspaceSendersApi.list(params, accessToken),
  })

  const enable = useMutation({
    mutationFn: (id: string) => workspaceSendersApi.enable(id, accessToken),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['senders'] })
      setNotice({ kind: 'ok', message: 'Sender enabled.' })
    },
    onError: (error: Error) => {
      setNotice({ kind: 'warn', message: error.message || 'Enable failed.' })
    },
  })

  const disable = useMutation({
    mutationFn: (id: string) => workspaceSendersApi.disable(id, accessToken),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['senders'] })
      setNotice({ kind: 'ok', message: 'Sender disabled.' })
    },
    onError: (error: Error) => {
      setNotice({ kind: 'warn', message: error.message || 'Disable failed.' })
    },
  })

  const items = senders.data?.items ?? []
  const total = senders.data?.total ?? 0
  const pageCount = Math.max(1, senders.data?.total_pages ?? Math.ceil(total / pageSize))

  const changeFilter = (setter: (value: string) => void, value: string) => {
    setter(value)
    setPage(1)
  }

  return (
    <main className="dashboard-content">
      <div className="page-heading">
        <div>
          <p className="eyebrow">EMAIL SENDING</p>
          <h1>Senders</h1>
          <p className="muted">
            Sender identities derived from your workspace mailboxes. Enable a sender to make it
            eligible for campaigns; availability is computed from the sender, mailbox, and
            provider connection state.
          </p>
        </div>
        <Link className="button button-secondary" to="/settings/email-providers/mailboxes">
          <Mail size={15} />
          Create senders
        </Link>
      </div>

      {notice && (
        <div className={`notice notice-${notice.kind}`} role="status">
          <span>{notice.message}</span>
          <button className="icon-button" onClick={() => setNotice(null)} aria-label="Dismiss">
            <span aria-hidden="true">×</span>
          </button>
        </div>
      )}

      <section className="card">
        <div className="card-header">
          <div>
            <h2>Sender Directory</h2>
            <p className="muted">
              Search, filter, and review sending availability across providers and mailboxes.
            </p>
          </div>
        </div>

        <div className="mailbox-toolbar">
          <label className="report-filter">
            <Search size={15} />
            <input
              type="search"
              value={search}
              onChange={(event) => changeFilter(setSearch, event.target.value)}
              placeholder="Search email or display name…"
              aria-label="Search senders"
            />
          </label>
          <label className="report-filter">
            <ShieldCheck size={15} />
            <select value={statusFilter} onChange={(event) => changeFilter(setStatusFilter, event.target.value)} aria-label="Sender status">
              {STATUS_FILTERS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label className="report-filter">
            <Mail size={15} />
            <select value={providerFilter} onChange={(event) => changeFilter(setProviderFilter, event.target.value)} aria-label="Provider">
              {PROVIDER_FILTERS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label className="report-filter">
            <ShieldCheck size={15} />
            <select value={sendingFilter} onChange={(event) => changeFilter(setSendingFilter, event.target.value)} aria-label="Sending state">
              {SENDING_FILTERS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label className="report-filter">
            <ShieldCheck size={15} />
            <select value={healthFilter} onChange={(event) => changeFilter(setHealthFilter, event.target.value)} aria-label="Health status">
              {HEALTH_FILTERS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label className="report-filter">
            <ShieldCheck size={15} />
            <select value={sort} onChange={(event) => changeFilter(setSort, event.target.value)} aria-label="Sort senders">
              {SORT_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label className="page-size-control">
            Show
            <select value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1) }} aria-label="Senders per page">
              <option value="25">25</option>
              <option value="50">50</option>
              <option value="100">100</option>
              <option value="200">200</option>
            </select>
          </label>
        </div>

        {senders.isLoading && (
          <div className="table-state">
            <Mail size={28} />
            <strong>Loading senders…</strong>
          </div>
        )}

        {senders.isError && (
          <div className="table-state error-state" role="alert">
            <Mail size={28} />
            <strong>Unable to load senders</strong>
            <span>{senders.error instanceof Error ? senders.error.message : 'Try again shortly.'}</span>
          </div>
        )}

        {!senders.isLoading && !senders.isError && items.length === 0 && (
          <div className="table-state empty-state">
            <ShieldCheck size={28} />
            <strong>{total === 0 ? 'No senders yet' : 'No senders match'}</strong>
            <p className="muted">
              {total === 0
                ? 'Create sender records from the mailboxes discovered in your connected workspaces.'
                : 'Adjust your search or filters.'}
            </p>
            <Link className="outline-button" to="/settings/email-providers/mailboxes">
              Create senders
            </Link>
          </div>
        )}

        {items.length > 0 && (
          <>
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Sender</th>
                    <th>Provider</th>
                    <th>Status</th>
                    <th>Sending</th>
                    <th>Health</th>
                    <th>Availability</th>
                    <th>Updated</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {items.map((sender: WorkspaceSender) => (
                    <tr key={sender.id}>
                      <td>
                        <div className="mailbox-cell">
                          <strong>{sender.display_name ?? sender.email}</strong>
                          <span className="muted small">{sender.email}</span>
                        </div>
                      </td>
                      <td>
                        <span className="provider-pill">{sender.provider}</span>
                      </td>
                      <td>
                        <span className={`status-pill status-${statusTone(sender.status)}`}>
                          <span />
                          {statusLabel(sender.status)}
                        </span>
                      </td>
                      <td>
                        <span className={`status-pill status-${sender.sending_enabled ? 'ok' : 'off'}`}>
                          <span />
                          {sender.sending_enabled ? 'Enabled' : 'Disabled'}
                        </span>
                      </td>
                      <td>
                        <div className="sender-health-cell">
                          {sender.health_status ? (
                            <span className={`status-pill status-${healthTone(sender.health_status)}`}>
                              <span />
                              {healthLabel(sender.health_status)}
                            </span>
                          ) : (
                            <span className="muted small">—</span>
                          )}
                          {sender.health_score != null && (
                            <span className="health-score" title={`Health score ${Math.round(sender.health_score)} / 100`}>
                              {Math.round(sender.health_score)}
                            </span>
                          )}
                        </div>
                      </td>
                      <td>
                        <span
                          className={`status-pill status-${sender.availability.available ? 'ok' : 'warn'}`}
                          title={`${sender.availability.available ? 'Eligible for sending' : availabilityLabel(sender.availability.reason)}`}
                        >
                          <span />
                          {sender.availability.available ? 'Available' : availabilityLabel(sender.availability.reason)}
                        </span>
                      </td>
                      <td className="muted small">{formatDateTime(sender.updated_at) ?? '—'}</td>
                      <td className="text-right">
                        <div className="row-gap row-gap-end">
                          {sender.status === 'REMOVED' ? (
                            <span className="muted small">—</span>
                          ) : (
                            <>
                              {sender.sending_enabled ? (
                                <button
                                  className="button button-secondary"
                                  onClick={() => disable.mutate(sender.id)}
                                  disabled={disable.isPending}
                                >
                                  Disable
                                </button>
                              ) : (
                                <button
                                  className="button"
                                  onClick={() => enable.mutate(sender.id)}
                                  disabled={enable.isPending}
                                >
                                  Enable
                                </button>
                              )}
                            </>
                          )}
                          <Link className="outline-button compact" to={`/senders/workspace/${sender.id}`}>
                            View
                          </Link>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <footer className="contact-pagination">
              <span>
                Page {page} of {pageCount} · {total} sender{total === 1 ? '' : 's'}
              </span>
              <div>
                <button
                  className="icon-button"
                  disabled={page <= 1}
                  onClick={() => setPage((current) => current - 1)}
                  aria-label="Previous page"
                >
                  <ChevronLeft size={17} />
                </button>
                <button
                  className="icon-button"
                  disabled={page >= pageCount}
                  onClick={() => setPage((current) => current + 1)}
                  aria-label="Next page"
                >
                  <ChevronRight size={17} />
                </button>
              </div>
            </footer>
          </>
        )}
      </section>
    </main>
  )
}