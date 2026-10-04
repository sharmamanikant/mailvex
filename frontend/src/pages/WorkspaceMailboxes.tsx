import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  ChevronLeft,
  ChevronRight,
  Cloud,
  Mail,
  RefreshCw,
  Search,
  UsersRound,
} from 'lucide-react'
import { providerConnectionsApi } from '../api/providerConnections'
import { workspaceMailboxesApi } from '../api/workspaceMailboxes'
import { workspaceSendersApi } from '../api/workspaceSenders'
import type { ProviderConnection } from '../types/providerConnections'
import type { WorkspaceMailbox } from '../types/workspaceMailboxes'

const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: '', label: 'All statuses' },
  { value: 'ACTIVE', label: 'Active' },
  { value: 'SUSPENDED', label: 'Suspended' },
  { value: 'DELETED', label: 'Deleted' },
]

function mailboxStatusTone(status: string): string {
  if (status === 'ACTIVE') return 'ok'
  if (status === 'SUSPENDED') return 'warn'
  if (status === 'DELETED') return 'off'
  return 'neutral'
}

function mailboxStatusLabel(status: string): string {
  return status.charAt(0) + status.slice(1).toLowerCase()
}

function syncingAny(connections: ProviderConnection[]): boolean {
  return connections.some((c) => c.last_sync_status === 'SYNCING')
}

function providerDisplayName(connection: ProviderConnection): string {
  const parts = [
    connection.provider_metadata.organizationName,
    connection.provider_metadata.defaultDomain ?? connection.workspace_domain,
  ].filter(Boolean)
  return parts.join(' · ') || connection.display_name || connection.id
}

function syncStatsSummary(stats: ProviderConnection['last_sync_stats'] | undefined, status: string | null): string | null {
  if (!stats || status !== 'COMPLETED') return null
  const parts: string[] = []
  if (stats.created) parts.push(`${stats.created} created`)
  if (stats.updated) parts.push(`${stats.updated} updated`)
  if (stats.suspended) parts.push(`${stats.suspended} suspended`)
  if (stats.deleted) parts.push(`${stats.deleted} deleted`)
  if (stats.skipped) parts.push(`${stats.skipped} skipped`)
  if (!parts.length) return null
  return ` · ${parts.join(', ')}`
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

function formatSyncStatus(status: string | null | undefined): string {
  if (status === 'SYNCING') return 'Syncing…'
  if (status === 'COMPLETED') return 'Completed'
  if (status === 'FAILED') return 'Failed'
  return 'Idle'
}

function useSelection() {
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())

  const toggleOne = (id: string) => {
    setSelectedIds((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const togglePage = (items: WorkspaceMailbox[], checked: boolean) => {
    setSelectedIds((current) => {
      const next = new Set(current)
      for (const item of items) {
        if (checked) next.add(item.id)
        else next.delete(item.id)
      }
      return next
    })
  }

  const clear = () => setSelectedIds(new Set())

  return { selectedIds, toggleOne, togglePage, clear }
}

export default function WorkspaceMailboxes({ accessToken }: { accessToken: string }) {
  const client = useQueryClient()
  const [connectionId, setConnectionId] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const [notice, setNotice] = useState<{ kind: 'ok' | 'warn'; message: string } | null>(null)
  const { selectedIds, toggleOne, togglePage, clear } = useSelection()

  const bulkCreate = useMutation({
    mutationFn: () =>
      workspaceSendersApi.bulkCreate(
        activeConnectionId as string,
        { selection_mode: 'EXPLICIT', mailbox_ids: [...selectedIds] },
        accessToken,
      ),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: ['providerConnections'] })
      void client.invalidateQueries({ queryKey: ['senders'] })
      setNotice({
        kind: 'ok',
        message: `Created ${result.created} sender${result.created === 1 ? '' : 's'} (${result.total_attempted} attempted, ${result.failed} failed).`,
      })
      clear()
    },
    onError: (error: Error) => {
      setNotice({ kind: 'warn', message: error.message || 'Sender creation failed.' })
    },
  })

  const runSync = useMutation({
    mutationFn: (id: string) => workspaceMailboxesApi.sync(id, accessToken),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['providerConnections'] })
      void client.invalidateQueries({ queryKey: ['mailboxes'] })
      setNotice({ kind: 'ok', message: 'Mailbox synchronization started.' })
    },
    onError: (error: Error) => {
      setNotice({ kind: 'warn', message: error.message || 'Synchronization failed.' })
    },
  })

  const connections = useQuery({
    queryKey: ['providerConnections'],
    queryFn: () => providerConnectionsApi.list(accessToken),
    refetchInterval: (query) =>
      (query.state.data ?? []).some((c) => c.last_sync_status === 'SYNCING') ? 5000 : false,
  })

  const connected = connections.data ?? []
  const activeConnectionId = connectionId ?? connected[0]?.id ?? null
  const polling = runSync.isPending || syncingAny(connections.data ?? [])
  const poll = polling ? 5000 : false as const

  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  })
  if (search.trim()) params.set('search', search.trim())
  if (statusFilter) params.set('status', statusFilter)

  const mailboxes = useQuery({
    queryKey: ['mailboxes', activeConnectionId, params.toString()],
    queryFn: () => workspaceMailboxesApi.list(activeConnectionId as string, params, accessToken),
    enabled: Boolean(activeConnectionId),
    refetchInterval: poll,
  })

  const selectedConnection = connected.find((c) => c.id === activeConnectionId) ?? null

  const items = mailboxes.data?.items ?? []
  const total = mailboxes.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / pageSize))

  const changeConnection = (id: string) => {
    setConnectionId(id)
    setPage(1)
    clear()
  }

  const changeFilter = (value: string) => {
    setStatusFilter(value)
    setPage(1)
    clear()
  }

  const changeSearch = (value: string) => {
    setSearch(value)
    setPage(1)
  }

  const changePageSize = (value: string) => {
    setPageSize(Number(value))
    setPage(1)
    clear()
  }

  const allVisibleSelected = items.length > 0 && items.every((item) => selectedIds.has(item.id))

  return (
    <main className="dashboard-content">
      <div className="page-heading">
        <div>
          <p className="eyebrow">EMAIL SENDING</p>
          <h1>Workspace Mailboxes</h1>
          <p className="muted">
            Mailboxes discovered from your connected provider directory. Sender records from
            selected mailboxes arrive in a later phase.
          </p>
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

      <section className="card">
        <div className="card-header">
          <div>
            <h2>Mailbox Directory</h2>
            <p className="muted">
              Sync a connected provider account to discover eligible mailboxes.
            </p>
          </div>
          <div className="row-gap">
            {selectedConnection && (
              <button
                className="button"
                onClick={() => runSync.mutate(selectedConnection.id)}
                disabled={runSync.isPending || !selectedConnection.credential_configured}
                title={
                  selectedConnection.credential_configured
                    ? 'Discover mailboxes now'
                    : 'Reconnect this workspace first'
                }
              >
                <RefreshCw size={15} />
                <span>{runSync.isPending ? 'Syncing…' : 'Sync mailboxes'}</span>
              </button>
            )}
          </div>
        </div>

        {connections.isLoading && (
          <div className="table-state">
            <Cloud size={28} />
            <strong>Loading providers…</strong>
          </div>
        )}

        {!connections.isLoading && connected.length === 0 && (
          <div className="table-state empty-state">
            <Cloud size={28} />
            <strong>No provider connected</strong>
            <p className="muted">
              Connect a Google Workspace or Microsoft 365 account before discovering mailboxes.
            </p>
            <Link className="outline-button" to="/settings/email-providers">
              Connect a provider
            </Link>
          </div>
        )}

        {connected.length > 0 && (
          <>
            <div className="mailbox-toolbar">
              <label className="report-filter">
                <Cloud size={15} />
                <select value={activeConnectionId ?? ''} onChange={(event) => changeConnection(event.target.value)} aria-label="Workspace">
                  {connected.map((connection) => (
                    <option key={connection.id} value={connection.id}>
                      {providerDisplayName(connection)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="report-filter">
                <Search size={15} />
                <input
                  type="search"
                  value={search}
                  onChange={(event) => changeSearch(event.target.value)}
                  placeholder="Search name, email, department…"
                  aria-label="Search mailboxes"
                />
              </label>
              <label className="report-filter">
                <UsersRound size={15} />
                <select value={statusFilter} onChange={(event) => changeFilter(event.target.value)} aria-label="Mailbox status">
                  {STATUS_FILTERS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="page-size-control">
                Show
                <select value={pageSize} onChange={(event) => changePageSize(event.target.value)} aria-label="Mailboxes per page">
                  <option value="25">25</option>
                  <option value="50">50</option>
                  <option value="100">100</option>
                  <option value="200">200</option>
                </select>
              </label>
            </div>

            {selectedConnection && selectedConnection.last_sync_status && (
              <p className="hint">
                <span className={`status-pill status-${mailboxStatusTone(selectedConnection.last_sync_status)}`}>
                  <span />
                  {formatSyncStatus(selectedConnection.last_sync_status)}
                </span>
                {selectedConnection.last_sync_completed_at && (
                  <span className="muted small">
                    last synced {formatDateTime(selectedConnection.last_sync_completed_at)}
                  </span>
                )}
                {syncStatsSummary(selectedConnection.last_sync_stats, selectedConnection.last_sync_status) && (
                  <span className="muted small">
                    {syncStatsSummary(selectedConnection.last_sync_stats, selectedConnection.last_sync_status)}
                  </span>
                )}
                {selectedConnection.last_sync_error && selectedConnection.last_sync_status === 'FAILED' && (
                  <span className="muted small">{selectedConnection.last_sync_error}</span>
                )}
              </p>
            )}

            {mailboxes.isLoading && (
              <div className="table-state">
                <Mail size={28} />
                <strong>Loading mailboxes…</strong>
              </div>
            )}

            {!mailboxes.isLoading && items.length === 0 && (
              <div className="table-state empty-state">
                <Mail size={28} />
                <strong>{total === 0 ? 'No mailboxes found' : 'No mailboxes match'}</strong>
                <p className="muted">
                  {total === 0
                    ? 'Run a sync to discover mailboxes from this workspace.'
                    : 'Adjust your search or filters.'}
                </p>
              </div>
            )}

            {items.length > 0 && (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>
                        <input
                          type="checkbox"
                          aria-label="Select all visible"
                          checked={allVisibleSelected}
                          onChange={(event) => togglePage(items, event.target.checked)}
                        />
                      </th>
                      <th>Mailbox</th>
                      <th>User type</th>
                      <th>Status</th>
                      <th>Department</th>
                      <th>Job title</th>
                      <th className="text-right">Discovered</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((mailbox) => (
                      <tr key={mailbox.id}>
                        <td>
                          <input
                            type="checkbox"
                            aria-label={`Select ${mailbox.email}`}
                            checked={selectedIds.has(mailbox.id)}
                            onChange={() => toggleOne(mailbox.id)}
                          />
                        </td>
                        <td>
                          <div className="mailbox-cell">
                            <strong>{mailbox.display_name ?? mailbox.email}</strong>
                            <span className="muted small">{mailbox.email}</span>
                          </div>
                        </td>
                        <td>
                          <span className="provider-pill">{mailbox.user_type}</span>
                        </td>
                        <td>
                          <span className={`status-pill status-${mailboxStatusTone(mailbox.provider_status)}`}>
                            <span />
                            {mailboxStatusLabel(mailbox.provider_status)}
                          </span>
                        </td>
                        <td className="muted small">{mailbox.department ?? '—'}</td>
                        <td className="muted small">{mailbox.job_title ?? '—'}</td>
                        <td className="text-right muted small">
                          {formatDateTime(mailbox.last_discovered_at) ?? '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {selectedIds.size > 0 && (
              <div className="bulk-bar" role="status">
                <span>{selectedIds.size} selected</span>
                <div className="row-gap">
                  <button
                    className="button button-secondary"
                    onClick={() => bulkCreate.mutate()}
                    disabled={bulkCreate.isPending}
                    title="Create sender records from the selected mailboxes"
                  >
                    {bulkCreate.isPending ? 'Creating…' : `Create senders (${selectedIds.size})`}
                  </button>
                  <button className="button button-secondary" onClick={clear}>
                    Clear selection
                  </button>
                </div>
              </div>
            )}

            {items.length > 0 && (
              <footer className="contact-pagination">
                <span>
                  Page {page} of {pageCount} · {total} mailbox{total === 1 ? '' : 'es'}
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
            )}
          </>
        )}
      </section>
    </main>
  )
}