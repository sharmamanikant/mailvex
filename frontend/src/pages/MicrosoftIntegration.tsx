import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ArrowLeft, CheckCircle2, Cloud, CircleAlert, KeyRound, Plug, RefreshCw, ShieldCheck } from 'lucide-react'
import { microsoftOAuthApi, senderConnectionsApi } from '../api/integrations'
import { redirectTo } from '../lib/navigation'

function humanStatus(status: string): string {
  return status.replace(/_/g, ' ')
}

function statusTone(status: string): string {
  if (status === 'CONNECTED') return 'ok'
  if (status === 'REAUTH_REQUIRED' || status === 'FAILED') return 'warn'
  if (status === 'DISCONNECTED' || status === 'DISABLED') return 'off'
  return 'neutral'
}

export default function MicrosoftIntegration({ accessToken }: { accessToken: string }) {
  const client = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [error, setError] = useState<string | null>(null)

  const connected = params.get('connected') === '1'
  const flowError = params.get('error')

  const dismissStatus = () => {
    setParams({}, { replace: true })
  }

  const connections = useQuery({ queryKey: ['connections'], queryFn: () => senderConnectionsApi.list(accessToken) })
  const connection = connections.data?.find((item) => item.provider === 'MICROSOFT')

  const startOAuth = useMutation({
    mutationFn: async () => {
      const connectionId = connection?.id
      if (!connectionId) {
        throw new Error('No Microsoft connection exists yet. Create one in Integrations first.')
      }
      const { authorization_url } = await microsoftOAuthApi.authorize({ connection_id: connectionId }, accessToken)
      redirectTo(authorization_url)
    },
    onError: (reason: Error) => setError(reason.message),
  })

  const invalidate = () => {
    void client.invalidateQueries({ queryKey: ['connections'] })
    void client.invalidateQueries({ queryKey: ['senders'] })
  }
  const runValidate = useMutation({
    mutationFn: () => microsoftOAuthApi.validate(connection!.id, accessToken),
    onSuccess: () => invalidate(),
    onError: (reason: Error) => setError(reason.message),
  })

  const needsSetup = !connection || connection.status === 'FAILED' || connection.status === 'DISCONNECTED'
  const needsReauth = connection?.status === 'REAUTH_REQUIRED'
  const isConnected = connection?.status === 'CONNECTED'

  return (
    <main className="integrations-page">
      <div className="senders-heading">
        <div>
          <Link className="back-link" to="/integrations"><ArrowLeft size={16} /> Back to integrations</Link>
          <p className="eyebrow">MICROSOFT 365</p>
          <h1>Microsoft sender authorization</h1>
          <p className="muted">Connect a Microsoft 365 / Exchange Online mailbox to authorize sending.</p>
        </div>
      </div>
      <div className="sender-notice">
        <ShieldCheck size={17} />
        <span>You will grant access with your Microsoft account. Tokens are encrypted at rest and scoped to sending only.</span>
      </div>
      {(connected || flowError) && (
        <div className={`table-state ${flowError ? 'error-state' : ''}`}>
          {flowError ? <CircleAlert size={16} /> : <CheckCircle2 size={16} />}
          {flowError === 'microsoft_oauth_error'
            ? 'Authorization failed. The flow was not completed — try again.'
            : flowError
              ? `Authorization error: ${flowError.replace(/_/g, ' ')}.`
              : 'Microsoft account connected. Your sender is ready to use.'}
          <button className="text-button" onClick={dismissStatus}>Dismiss</button>
        </div>
      )}
      {error && (
        <div className="table-state error-state" role="alert">
          <CircleAlert size={16} /> {error}
          <button className="text-button" onClick={() => setError(null)}>Dismiss</button>
        </div>
      )}

      <section className="integration-connections">
        <h2>Microsoft connection</h2>
        {connections.isLoading ? (
          <div className="table-state">Loading connections...</div>
        ) : connections.isError ? (
          <div className="table-state error-state">{connections.error.message}</div>
        ) : connection ? (
          <article className="sender-card">
            <div className="sender-icon"><Cloud size={19} /></div>
            <div className="sender-main">
              <div className="sender-title">
                <h2>{connection.email ?? connection.external_account_id ?? connection.provider}</h2>
                <span className={`sender-status sender-${statusTone(connection.status)}`}><i /> {humanStatus(connection.status)}</span>
              </div>
              <p><KeyRound size={13} /> Microsoft 365 / Exchange Online · OAuth</p>
              <div className="sender-meta">
                <span className={connection.credential_configured ? 'health-ok' : 'health-warning'}>
                  {connection.credential_configured ? 'Credentials configured' : 'No credentials stored'}
                </span>
                {connection.last_connected_at && <span>Last connected {new Date(connection.last_connected_at).toLocaleDateString()}</span>}
              </div>
            </div>
            <div className="sender-actions">
              {(needsSetup || needsReauth) && (
                <button className="primary-button compact" onClick={() => startOAuth.mutate()} disabled={startOAuth.isPending}>
                  <Plug size={14} /> {startOAuth.isPending ? 'Opening Microsoft...' : needsReauth ? 'Reconnect' : 'Connect Microsoft'}
                </button>
              )}
              {isConnected && (
                <button className="secondary-button compact" onClick={() => runValidate.mutate()} disabled={runValidate.isPending}>
                  <RefreshCw size={14} /> Re-check
                </button>
              )}
            </div>
          </article>
        ) : (
          <div className="table-state">
            <CircleAlert size={16} /> No Microsoft connection yet.{' '}
            <Link className="text-button" to="/integrations">Create one via Connect provider</Link>
          </div>
        )}
      </section>

      <section className="integration-connections">
        <h2>What happens next</h2>
        <div className="table-state">
          <p>1. A Microsoft consent page opens for your account.</p>
          <p>2. Grant access — only profile and sending permission is requested.</p>
          <p>3. You return here and your sender appears under Sender accounts.</p>
        </div>
      </section>
    </main>
  )
}
