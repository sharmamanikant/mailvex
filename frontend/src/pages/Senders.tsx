import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  CheckCircle2,
  CircleAlert,
  Cloud,
  Link2,
  Mail,
  RefreshCw,
  Send,
  ShieldCheck,
  Trash2,
} from 'lucide-react'
import { integrationsApi, senderConnectionsApi } from '../api/integrations'
import type { SenderAccount, SenderConnection } from '../types/integrations'

function humanStatus(status: string): string {
  return status.replace(/_/g, ' ')
}

function statusTone(status: string): string {
  if (status === 'ACTIVE' || status === 'CONNECTED') return 'ok'
  if (status === 'PENDING_VERIFICATION' || status === 'REAUTH_REQUIRED') return 'warn'
  if (status === 'INVALID' || status === 'FAILED' || status === 'SUSPENDED') return 'critical'
  return 'off'
}

export default function Senders({ accessToken }: { accessToken: string }) {
  const [notice, setNotice] = useState<string | null>(null)
  const connections = useQuery({ queryKey: ['connections'], queryFn: () => senderConnectionsApi.list(accessToken) })

  return (
    <main className="senders-page">
      <div className="senders-heading">
        <div>
          <p className="eyebrow">SENDER CENTER</p>
          <h1>Sender accounts</h1>
          <p className="muted">Mailboxes imported from your connected providers.</p>
        </div>
        <Link className="primary-button compact" to="/integrations">
          <Cloud size={16} /> Manage integrations
        </Link>
      </div>
      <div className="sender-notice">
        <ShieldCheck size={17} />
        <span>Sender accounts are registered against provider connections. Credentials stay encrypted and server-side.</span>
      </div>
      {notice && <div className="table-state error-state">{notice}</div>}
      {connections.isLoading ? (
        <div className="table-state">Loading senders...</div>
      ) : connections.isError ? (
        <div className="table-state error-state">{connections.error.message}</div>
      ) : connections.data?.length ? (
        <div className="sender-list">
          {connections.data.map((connection) => (
            <ConnectionSenders key={connection.id} connection={connection} accessToken={accessToken} onNotice={setNotice} />
          ))}
        </div>
      ) : (
        <div className="table-state">
          <CircleAlert size={16} /> No provider connections yet.{' '}
          <Link className="text-button" to="/integrations">Connect a provider to get started</Link>
        </div>
      )}
    </main>
  )
}

function ConnectionSenders({ connection, accessToken, onNotice }: { connection: SenderConnection; accessToken: string; onNotice: (message: string | null) => void }) {
  const client = useQueryClient()
  const senders = useQuery({ queryKey: ['senders', connection.id], queryFn: () => integrationsApi.listSenders(connection.id, accessToken) })

  const invalidate = () => {
    void client.invalidateQueries({ queryKey: ['senders', connection.id] })
    void client.invalidateQueries({ queryKey: ['connections'] })
  }

  const runValidate = useMutation({
    mutationFn: () => senderConnectionsApi.validate(connection.id, accessToken),
    onSuccess: () => {
      onNotice(`${connection.email ?? connection.provider} validated — ${humanStatus(connection.status)}.`)
      invalidate()
    },
    onError: (error: Error) => onNotice(error.message),
  })
  const runDelete = useMutation({
    mutationFn: () => senderConnectionsApi.delete(connection.id, accessToken),
    onSuccess: () => {
      onNotice('Connection deleted.')
      invalidate()
    },
    onError: (error: Error) => onNotice(error.message),
  })

  return (
    <section className="connection-group">
      <div className="connection-heading">
        <div className="sender-icon"><Cloud size={18} /></div>
        <div className="sender-main">
          <div className="sender-title">
            <h2>{connection.email ?? connection.external_account_id ?? connection.provider}</h2>
            <span className={`sender-status sender-${statusTone(connection.status)}`}><i /> {humanStatus(connection.status)}</span>
          </div>
          <div className="sender-meta">
            <span>{connection.provider}</span>
            <span>{connection.connection_type}</span>
            {connection.credential_configured && <span className="health-ok">Credentials configured</span>}
          </div>
        </div>
        <div className="sender-actions">
          <button className="secondary-button compact" onClick={() => runValidate.mutate()}>
            <RefreshCw size={14} /> Validate
          </button>
          <button className="danger-button compact" onClick={() => runDelete.mutate()}>
            <Trash2 size={14} /> Delete
          </button>
        </div>
      </div>
      {senders.isLoading ? (
        <div className="table-state">Loading sender accounts...</div>
      ) : senders.isError ? (
        <div className="table-state error-state">{senders.error.message}</div>
      ) : senders.data?.length ? (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>From address</th>
                <th>Display name</th>
                <th>Status</th>
                <th>Health</th>
                <th>Last used</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {senders.data.map((sender) => (
                <SenderRow key={sender.id} sender={sender} accessToken={accessToken} onNotice={onNotice} />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="table-state">No sender accounts on this connection. Use Discover senders in Integrations to import mailboxes.</div>
      )}
    </section>
  )
}

function SenderRow({ sender, accessToken, onNotice }: { sender: SenderAccount; accessToken: string; onNotice: (message: string | null) => void }) {
  const client = useQueryClient()
  const invalidate = () => {
    void client.invalidateQueries({ queryKey: ['senders', sender.connection_id] })
    void client.invalidateQueries({ queryKey: ['connections'] })
  }
  const toggle = useMutation({
    mutationFn: (status: string) =>
      status === 'DISABLED'
        ? integrationsApi.enableSender(sender.id, accessToken)
        : integrationsApi.disableSender(sender.id, accessToken),
    onSuccess: (account) => {
      onNotice(`${account.email} ${account.status === 'DISABLED' ? 'disabled' : 'enabled'}.`)
      invalidate()
    },
    onError: (error: Error) => onNotice(error.message),
  })
  const runTest = useMutation({
    mutationFn: async () => {
      const recipient = window.prompt('Send a test email to:', '')
      if (!recipient) return null
      return integrationsApi.sendTestEmail(sender.id, { recipient }, accessToken)
    },
    onSuccess: (result) => {
      if (result) {
        onNotice(`Test email sent to ${result.recipient} (${result.message_id}).`)
        invalidate()
      }
    },
    onError: (error: Error) => onNotice(error.message),
  })
  return (
    <tr>
      <td><span className="row-primary"><Mail size={13} /> {sender.email}</span></td>
      <td>{sender.display_name ?? '—'}</td>
      <td>
        <span className={`sender-status sender-${statusTone(sender.status)}`}><i /> {humanStatus(sender.status)}</span>
      </td>
      <td>
        <span className={sender.health_status === 'HEALTHY' ? 'health-ok' : 'health-warning'}>
          {sender.health_status ? humanStatus(sender.health_status) : 'UNKNOWN'}
        </span>
      </td>
      <td>{sender.last_used_at ? new Date(sender.last_used_at).toLocaleDateString() : 'Never'}</td>
      <td>
        <div className="sender-actions">
          {sender.status === 'ACTIVE' && (
            <button className="secondary-button compact" onClick={() => runTest.mutate()} disabled={runTest.isPending}>
              <Send size={14} /> {runTest.isPending ? 'Sending...' : 'Test'}
            </button>
          )}
          {sender.status === 'ACTIVE' ? (
            <button className="secondary-button compact" onClick={() => toggle.mutate('ACTIVE')}>
              <CircleAlert size={14} /> Disable
            </button>
          ) : sender.status === 'DISABLED' ? (
            <button className="secondary-button compact" onClick={() => toggle.mutate('DISABLED')}>
              <CheckCircle2 size={14} /> Enable
            </button>
          ) : (
            <span className="health-caption"><Link2 size={12} /> {humanStatus(sender.status)}</span>
          )}
        </div>
      </td>
    </tr>
  )
}