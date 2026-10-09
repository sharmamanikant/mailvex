import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams, Link } from 'react-router-dom'
import { CheckCircle2, CircleAlert, Cloud, Link2, Puzzle, RefreshCw, Trash2 } from 'lucide-react'
import type { ReactNode } from 'react'
import type { ProviderConnection } from '../types/providerConnections'
import { providerConnectionsApi } from '../api/providerConnections'
import { redirectTo } from '../lib/navigation'

const errorMessages: Record<string, string> = {
  access_denied: 'Connection cancelled. Sign-in was not completed.',
  state_expired: 'This connection request expired. Please try again.',
  state_invalid: 'This connection request is no longer valid. Please try again.',
  duplicate: 'A connection for this workspace already exists.',
  insufficient_scope: 'Required permissions were not granted.',
  oauth_failed: 'Authorization failed. Please try again.',
  verify_failed: 'Your account could not be verified for this provider.',
  not_configured: 'OAuth is not configured for this workspace.',
  admin_consent_required: 'Microsoft 365 admin approval is required. Ask a tenant admin to grant consent.',
  refresh_failed: 'Credential refresh failed. Try again shortly.',
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    CONNECTING: 'Connecting…',
    CONNECTED: 'Connected',
    ERROR: 'Error',
    DISCONNECTED: 'Disconnected',
    REVOKED: 'Revoked',
  }
  return labels[status] ?? status
}

function formatExpiry(value: string | null): string | null {
  if (!value) return null
  if (!Number.isNaN(Date.parse(value))) {
    return new Date(value).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
  }
  return null
}

function statusTone(status: string): string {
  if (status === 'CONNECTED') return 'ok'
  if (status === 'ERROR' || status === 'REVOKED') return 'warn'
  if (status === 'DISCONNECTED') return 'off'
  return 'neutral'
}

interface RefreshDisconnectMutations {
  refresh: {
    isPending: boolean
    mutate: (id: string) => void
  }
  disconnect: {
    isPending: boolean
    mutate: (id: string) => void
  }
}

function ProviderCard({
  title,
  description,
  rows,
  connecting,
  startPending,
  startLabel,
  emptyMessage,
  workspaceCell,
  onStart,
  mutations,
}: {
  title: string
  description: ReactNode
  rows: ProviderConnection[]
  connecting: boolean
  startPending: boolean
  startLabel: string
  emptyMessage: string
  workspaceCell: (connection: ProviderConnection) => ReactNode
  onStart: () => void
  mutations: RefreshDisconnectMutations
}) {
  const connectedRows = rows.filter((connection) => connection.status === 'CONNECTED')
  return (
    <section className="card">
      <div className="card-header">
        <div>
          <h2>{title}</h2>
          <p className="muted">{description}</p>
        </div>
        <div className="row-gap">
          <Link className="button button-secondary" to="/settings/email-providers/mailboxes">
            <Puzzle size={16} />
            <span>View mailboxes</span>
          </Link>
          <button
            className="button"
            onClick={onStart}
            disabled={connecting || startPending}
          >
            <Link2 size={16} />
            <span>{connecting ? 'Connecting…' : startLabel}</span>
          </button>
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="table-state empty-state">
          <Puzzle size={28} />
          <strong>No provider linked</strong>
          <p className="muted">{emptyMessage}</p>
        </div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Provider</th>
              <th>Workspace</th>
              <th>Status</th>
              <th>Scopes</th>
              <th className="text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((connection) => (
              <tr key={connection.id}>
                <td>
                  <span className="provider-pill">
                    <Cloud size={15} />
                    {title}
                  </span>
                </td>
                <td>{workspaceCell(connection)}</td>
                <td>
                  <span className={`status-pill status-${statusTone(connection.status)}`}>
                    <span />
                    {statusLabel(connection.status)}
                  </span>
                  {connection.status === 'CONNECTED' && formatExpiry(connection.credential_expires_at) && (
                    <span className="credential-expiry muted small">expires {formatExpiry(connection.credential_expires_at)}</span>
                  )}
                </td>
                <td className="muted small">{connection.scopes.length || '—'}</td>
                <td className="text-right">
                  <div className="row-gap row-gap-end">
                    {(connection.status === 'CONNECTED' || connection.status === 'ERROR') && (
                      <button
                        className="button button-secondary"
                        onClick={() => mutations.refresh.mutate(connection.id)}
                        disabled={!connection.credential_configured || mutations.refresh.isPending}
                        title={connection.credential_configured ? 'Refresh credentials now' : 'No stored credentials to refresh'}
                      >
                        <RefreshCw size={15} />
                        <span>Refresh</span>
                      </button>
                    )}
                    {connection.status === 'CONNECTED' && (
                      <button
                        className="button button-danger"
                        onClick={() => mutations.disconnect.mutate(connection.id)}
                        disabled={mutations.disconnect.isPending}
                      >
                        <Trash2 size={15} />
                        <span>Disconnect</span>
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {connectedRows.length > 0 && (
        <p className="hint muted">
          <CheckCircle2 size={14} aria-hidden="true" />
          Green status means {title} access is active. Sending still happens per-mailbox in a later phase.
        </p>
      )}
    </section>
  )
}

export default function EmailProviders({ accessToken }: { accessToken: string }) {
  const client = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const statusParam = searchParams.get('status')
  const errorParam = searchParams.get('error')

  const notice = (() => {
    if (statusParam === 'connected') return { kind: 'ok' as const, message: 'Provider connected successfully.' }
    if (statusParam === 'refreshed') return { kind: 'ok' as const, message: 'Credential refreshed successfully.' }
    if (statusParam === 'error' && errorParam) return { kind: 'warn' as const, message: errorMessages[errorParam] ?? errorParam }
    return null
  })()

  const clearNotice = () => {
    if (notice) setSearchParams({}, { replace: true })
  }

  const connections = useQuery({ queryKey: ['providerConnections'], queryFn: () => providerConnectionsApi.list(accessToken) })

  const startGoogle = useMutation({
    mutationFn: () => providerConnectionsApi.startGoogle(accessToken),
    onSuccess: (result) => {
      redirectTo(result.authorization_url)
    },
    onError: (error: Error) => {
      setSearchParams({ status: 'error', error: 'oauth_failed' }, { replace: true })
      console.error('start google connection failed:', error.message)
    },
  })

  const startMicrosoft = useMutation({
    mutationFn: () => providerConnectionsApi.startMicrosoft(accessToken),
    onSuccess: (result) => {
      redirectTo(result.authorization_url)
    },
    onError: (error: Error) => {
      setSearchParams({ status: 'error', error: 'oauth_failed' }, { replace: true })
      console.error('start microsoft connection failed:', error.message)
    },
  })

  const runDisconnect = useMutation({
    mutationFn: (id: string) => providerConnectionsApi.disconnect(id, accessToken),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['providerConnections'] }),
    onError: (error: Error) => {
      setSearchParams({ status: 'error', error: 'oauth_failed' }, { replace: true })
      console.error('disconnect failed:', error.message)
    },
  })

  const runRefresh = useMutation({
    mutationFn: (id: string) => providerConnectionsApi.refresh(id, accessToken),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['providerConnections'] })
      setSearchParams({ status: 'refreshed' }, { replace: true })
    },
    onError: (error: Error) => {
      setSearchParams({ status: 'error', error: 'refresh_failed' }, { replace: true })
      console.error('refresh failed:', error.message)
    },
  })

  const rows = connections.data ?? []
  const googleRows = rows.filter((connection) => connection.provider === 'GOOGLE')
  const microsoftRows = rows.filter((connection) => connection.provider === 'MICROSOFT')
  const googleConnecting = googleRows.some((connection) => connection.status === 'CONNECTING')
  const microsoftConnecting = microsoftRows.some((connection) => connection.status === 'CONNECTING')

  const mutations: RefreshDisconnectMutations = {
    refresh: runRefresh,
    disconnect: runDisconnect,
  }

  const googleWorkspaceCell = (connection: ProviderConnection) => (
    <>{connection.workspace_domain ?? connection.display_name ?? '—'}</>
  )

  const microsoftWorkspaceCell = (connection: ProviderConnection) => {
    const orgName = connection.provider_metadata.organizationName
    const domain = connection.provider_metadata.defaultDomain ?? connection.workspace_domain
    const tenantId = connection.provider_metadata.microsoftTenantId
    return (
      <>
        {orgName ?? domain ?? connection.display_name ?? '—'}
        {(domain || tenantId) && (
          <div className="muted small">
            {[domain, tenantId].filter(Boolean).join(' · ')}
          </div>
        )}
      </>
    )
  }

  return (
    <main className="dashboard-content">
      <div className="page-heading">
        <div>
          <p className="eyebrow">EMAIL SENDING</p>
          <h1>Email Providers</h1>
          <p className="muted">Connect the Google Workspace or Microsoft 365 accounts your team sends from.</p>
        </div>
      </div>

      {notice && (
        <div className={`notice notice-${notice.kind}`} role="status">
          <span>{notice.message}</span>
          <button className="icon-button" onClick={clearNotice} aria-label="Dismiss"><span aria-hidden="true">×</span></button>
        </div>
      )}

      <ProviderCard
        title="Google Workspace"
        description={
          <>
            Authorize directory access so mailboxes can be discovered for sending.
            Requires a Google Workspace admin account.
          </>
        }
        rows={googleRows}
        connecting={googleConnecting}
        startPending={startGoogle.isPending}
        startLabel="Connect Google Workspace"
        emptyMessage="Connect your first Google Workspace account to begin mailbox setup."
        workspaceCell={googleWorkspaceCell}
        onStart={() => startGoogle.mutate()}
        mutations={mutations}
      />

      <ProviderCard
        title="Microsoft 365"
        description={
          <>
            Authorize Microsoft Graph directory access so mailboxes can be discovered for sending.
            Sign in with a Microsoft 365 admin account and tenant-wide consent.
          </>
        }
        rows={microsoftRows}
        connecting={microsoftConnecting}
        startPending={startMicrosoft.isPending}
        startLabel="Connect Microsoft 365"
        emptyMessage="Connect your first Microsoft 365 tenant to begin mailbox setup."
        workspaceCell={microsoftWorkspaceCell}
        onStart={() => startMicrosoft.mutate()}
        mutations={mutations}
      />

      <p className="hint muted">
        <CircleAlert size={14} aria-hidden="true" />
        We only ever store encrypted credentials. Raw tokens never appear in this page or in audit logs.
      </p>
    </main>
  )
}
