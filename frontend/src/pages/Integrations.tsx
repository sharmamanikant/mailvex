import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  CheckCircle2,
  CircleAlert,
  Cloud,
  KeyRound,
  Link2,
  Plug,
  RefreshCw,
  Search,
  ShieldCheck,
  Trash2,
  X,
} from 'lucide-react'
import { integrationsApi, providersApi, senderConnectionsApi } from '../api/integrations'
import type { ConnectionType, CredentialUpload, ProviderCapability } from '../types/integrations'

const typeLabels: Record<string, string> = { OAUTH: 'OAuth', API_KEY: 'API key', SMTP: 'SMTP' }
const providerIcons: Record<string, typeof Cloud> = {
  GOOGLE: Cloud,
  MICROSOFT: Cloud,
  SMTP: ShieldCheck,
}

function humanStatus(status: string): string {
  return status.replace(/_/g, ' ')
}

function statusTone(status: string): string {
  if (status === 'CONNECTED') return 'ok'
  if (status === 'REAUTH_REQUIRED' || status === 'FAILED') return 'warn'
  if (status === 'DISCONNECTED' || status === 'DISABLED') return 'off'
  return 'neutral'
}

export default function Integrations({ accessToken }: { accessToken: string }) {
  const client = useQueryClient()
  const navigate = useNavigate()
  const [wizard, setWizard] = useState<ProviderCapability | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [discovery, setDiscovery] = useState<{ connectionId: string; senders: Array<{ email: string; display_name: string | null; external_sender_id: string | null }> } | null>(null)

  const invalidate = () => {
    void client.invalidateQueries({ queryKey: ['connections'] })
    void client.invalidateQueries({ queryKey: ['senders'] })
  }

  const providers = useQuery({ queryKey: ['providers'], queryFn: () => providersApi.list(accessToken) })
  const connections = useQuery({ queryKey: ['connections'], queryFn: () => senderConnectionsApi.list(accessToken) })

  const runValidate = useMutation({
    mutationFn: (connectionId: string) => senderConnectionsApi.validate(connectionId, accessToken),
    onSuccess: (connection) => {
      setNotice(`${connection.email ?? 'Connection'} validated — ${humanStatus(connection.status)}.`)
      invalidate()
    },
    onError: (error: Error) => setNotice(error.message),
  })
  const runDiscover = useMutation({
    mutationFn: (connectionId: string) => senderConnectionsApi.discover(connectionId, accessToken),
    onSuccess: (result, connectionId) => setDiscovery({ connectionId, senders: result.senders }),
    onError: (error: Error) => setNotice(error.message),
  })
  const runDisconnect = useMutation({
    mutationFn: (connectionId: string) => senderConnectionsApi.disconnect(connectionId, accessToken),
    onSuccess: () => {
      setDiscovery(null)
      invalidate()
    },
    onError: (error: Error) => setNotice(error.message),
  })
  const runDelete = useMutation({
    mutationFn: (connectionId: string) => senderConnectionsApi.delete(connectionId, accessToken),
    onSuccess: () => {
      setDiscovery(null)
      invalidate()
    },
    onError: (error: Error) => setNotice(error.message),
  })

  return (
    <main className="integrations-page">
      <div className="senders-heading">
        <div>
          <p className="eyebrow">INTEGRATIONS</p>
          <h1>Email provider integrations</h1>
          <p className="muted">Connect providers and manage credentials for future sending.</p>
        </div>
      </div>
      <div className="sender-notice">
        <ShieldCheck size={17} />
        <span>Credentials are encrypted at rest and stored server-side. Secrets are accepted once and never returned.</span>
      </div>
      {notice && <div className="table-state error-state">{notice}</div>}

      <section className="integration-catalog">
        <h2>Available providers</h2>
        {providers.isLoading ? (
          <div className="table-state">Loading providers...</div>
        ) : providers.isError ? (
          <div className="table-state error-state">{providers.error.message}</div>
        ) : providers.data?.length ? (
          <div className="provider-grid">
            {providers.data.map((provider) => {
              const Icon = providerIcons[provider.provider_name] ?? Plug
              return (
                <article className="provider-card" key={provider.provider_name}>
                  <div className="provider-icon"><Icon size={20} /></div>
                  <div className="provider-main">
                    <h2>{provider.display_name}</h2>
                    <p>{provider.provider_name}</p>
                  </div>
                  <ul className="capability-list">
                    {provider.supports_oauth && <Capability icon={KeyRound} label="OAuth" />}
                    {provider.supports_api_key && <Capability icon={KeyRound} label="API key" />}
                    {provider.supports_smtp && <Capability icon={ShieldCheck} label="SMTP" />}
                    {provider.supports_sender_discovery && <Capability icon={Search} label="Sender discovery" />}
                    {provider.supports_webhooks && <Capability icon={Link2} label="Webhooks" />}
                    {provider.supports_inbox_sync && <Capability icon={RefreshCw} label="Inbox sync" />}
                  </ul>
                  <button
                    className="primary-button compact"
                    onClick={() =>
                      provider.provider_name === 'GOOGLE'
                        ? navigate('/integrations/google')
                        : provider.provider_name === 'MICROSOFT'
                          ? navigate('/integrations/microsoft')
                          : setWizard(provider)
                    }
                  >
                    <Plug size={15} /> Connect
                  </button>
                </article>
              )
            })}
          </div>
        ) : (
          <div className="table-state">No providers available.</div>
        )}
      </section>

      <section className="integration-connections">
        <h2>Your connections</h2>
        {connections.isLoading ? (
          <div className="table-state">Loading connections...</div>
        ) : connections.isError ? (
          <div className="table-state error-state">{connections.error.message}</div>
        ) : connections.data?.length ? (
          <div className="sender-list">
            {connections.data.map((connection) => {
              const configured = connection.credential_configured
              return (
                <article className="sender-card" key={connection.id}>
                  <div className="sender-icon"><Cloud size={19} /></div>
                  <div className="sender-main">
                    <div className="sender-title">
                      <h2>{connection.email ?? connection.external_account_id ?? connection.provider}</h2>
                      <span className={`sender-status sender-${statusTone(connection.status)}`}>
                        <i /> {humanStatus(connection.status)}
                      </span>
                    </div>
                    <p><Link2 size={13} /> {connection.provider} · {typeLabels[connection.connection_type] ?? connection.connection_type}</p>
                    <div className="sender-meta">
                      <span className={configured ? 'health-ok' : 'health-warning'}>
                        {configured ? 'Credentials configured' : 'No credentials stored'}
                      </span>
                      {connection.last_connected_at && <span>Connected {new Date(connection.last_connected_at).toLocaleDateString()}</span>}
                    </div>
                  </div>
                  <div className="sender-actions">
                    {(connection.status === 'REAUTH_REQUIRED') && (connection.provider === 'GOOGLE' || connection.provider === 'MICROSOFT') && (
                      <button className="primary-button compact" onClick={() => navigate(`/integrations/${connection.provider.toLowerCase()}`)}>
                        <RefreshCw size={14} /> Reconnect
                      </button>
                    )}
                    <button className="secondary-button compact" onClick={() => runValidate.mutate(connection.id)}>
                      <CheckCircle2 size={14} /> Validate
                    </button>
                    <button className="secondary-button compact" onClick={() => runDiscover.mutate(connection.id)}>
                      <Search size={14} /> Discover senders
                    </button>
                    <button className="secondary-button compact" onClick={() => runDisconnect.mutate(connection.id)}>
                      <RefreshCw size={14} /> Disconnect
                    </button>
                    <button className="danger-button compact" onClick={() => runDelete.mutate(connection.id)}>
                      <Trash2 size={14} /> Delete
                    </button>
                  </div>
                </article>
              )
            })}
          </div>
        ) : (
          <div className="table-state">
            <CircleAlert size={16} /> No connections yet. Select a provider above to begin.
          </div>
        )}
      </section>

      {discovery && (
        <section className="integration-connections">
          <h2>Discovered senders</h2>
          {discovery.senders.length ? (
            <div className="sender-list">
              {discovery.senders.map((sender) => (
                <article className="sender-card" key={sender.email}>
                  <div className="sender-icon"><Link2 size={19} /></div>
                  <div className="sender-main">
                    <div className="sender-title"><h2>{sender.display_name ?? sender.email}</h2></div>
                    <p><MailTiny email={sender.email} /></p>
                  </div>
                  <div className="sender-actions">
                    <ImportSendersButton connectionId={discovery.connectionId} senders={discovery.senders} accessToken={accessToken} onDone={invalidate} />
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="table-state">No senders discovered on this connection.</div>
          )}
          <button className="text-button" onClick={() => setDiscovery(null)}>Dismiss</button>
        </section>
      )}

      {wizard && (
        <ConnectWizard
          provider={wizard}
          accessToken={accessToken}
          onClose={() => setWizard(null)}
          onCreated={() => {
            setWizard(null)
            setDiscovery(null)
            invalidate()
            setNotice(`${wizard.display_name} connection created.`)
          }}
        />
      )}
    </main>
  )
}

function Capability({ icon: Icon, label }: { icon: typeof KeyRound; label: string }) {
  return <li><Icon size={13} /> {label}</li>
}

function MailTiny({ email }: { email: string }) {
  return <span className="sender-meta-row"><Link2 size={13} /> {email}</span>
}

function ImportSendersButton({ connectionId, senders, accessToken, onDone }: { connectionId: string; senders: Array<{ email: string; display_name: string | null; external_sender_id: string | null }>; accessToken: string; onDone: () => void }) {
  const client = useQueryClient()
  const importMutation = useMutation({
    mutationFn: () => integrationsApi.importSenders({ connection_id: connectionId, senders }, accessToken),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['senders'] })
      void client.invalidateQueries({ queryKey: ['connections'] })
      onDone()
    },
  })
  return (
    <button className="primary-button compact" onClick={() => importMutation.mutate()}>
      <CheckCircle2 size={14} /> Import {senders.length} sender{senders.length === 1 ? '' : 's'}
    </button>
  )
}

function ConnectWizard({ provider, accessToken, onClose, onCreated }: { provider: ProviderCapability; accessToken: string; onClose: () => void; onCreated: () => void }) {
  const [connectionType, setConnectionType] = useState<ConnectionType>(() => {
    if (provider.supports_smtp) return 'SMTP'
    if (provider.supports_oauth) return 'OAUTH'
    return 'API_KEY'
  })
  const [email, setEmail] = useState('')
  const [externalAccountId, setExternalAccountId] = useState('')
  const [smtpHost, setSmtpHost] = useState('')
  const [smtpPort, setSmtpPort] = useState('587')
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({})
  const [error, setError] = useState('')
  const client = useQueryClient()

  const submit = useMutation({
    mutationFn: async () => {
      const connection = await senderConnectionsApi.create(
        {
          provider: provider.provider_name,
          connection_type: connectionType,
          email: email || null,
          external_account_id: externalAccountId || null,
          smtp_host: smtpHost || null,
          smtp_port: smtpHost ? Number(smtpPort) || null : null,
          smtp_username: fieldValues.smtp_username || null,
        },
        accessToken,
      )
      const credential: CredentialUpload = {}
      if (connectionType === 'API_KEY') credential.api_key = fieldValues.api_key ?? null
      if (connectionType === 'SMTP') credential.smtp_password = fieldValues.smtp_password ?? null
      if (connectionType === 'OAUTH') {
        credential.client_id = fieldValues.client_id ?? null
        credential.client_secret = fieldValues.client_secret ?? null
        credential.tenant_id = fieldValues.tenant_id ?? null
      }
      if (Object.values(credential).some((value) => value)) {
        await integrationsApi.storeCredentials(connection.id, credential, accessToken)
      }
      void client.invalidateQueries({ queryKey: ['connections'] })
    },
    onSuccess: onCreated,
    onError: (reason: Error) => setError(reason.message),
  })

  const availableTypes = provider.connection_types
  const setField = (key: string) => (event: React.ChangeEvent<HTMLInputElement>) => {
    setFieldValues((previous) => ({ ...previous, [key]: event.target.value }))
  }

  return (
    <div className="modal-backdrop">
      <section className="sender-modal">
        <div className="modal-heading">
          <div>
            <p className="eyebrow">CONNECT PROVIDER</p>
            <h2>{provider.display_name}</h2>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>
        <p className="modal-description">Credentials are transmitted once and encrypted at rest. They are never readable again.</p>
        <form onSubmit={(event) => { event.preventDefault(); submit.mutate() }}>
          {availableTypes.length > 1 && (
            <label className="form-field">
              <span>Connection type</span>
              <select value={connectionType} onChange={(event) => setConnectionType(event.target.value as ConnectionType)}>
                {availableTypes.map((type) => <option key={type} value={type}>{typeLabels[type] ?? type}</option>)}
              </select>
            </label>
          )}
          {connectionType !== 'SMTP' && (
            <label className="form-field">
              <span>Account email</span>
              <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="sender@company.com" />
            </label>
          )}
          {connectionType === 'SMTP' && (
            <>
              <label className="form-field">
                <span>From address *</span>
                <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="sender@company.com" />
              </label>
              <label className="form-field">
                <span>SMTP host *</span>
                <input value={smtpHost} onChange={(event) => setSmtpHost(event.target.value)} placeholder="smtp.company.com" />
              </label>
              <label className="form-field">
                <span>SMTP port</span>
                <input type="number" value={smtpPort} onChange={(event) => setSmtpPort(event.target.value)} />
              </label>
            </>
          )}
          <label className="form-field">
            <span>External account ID</span>
            <input value={externalAccountId} onChange={(event) => setExternalAccountId(event.target.value)} placeholder="Optional" />
          </label>
          {connectionType === 'SMTP' && (
            <label className="form-field">
              <span>SMTP username</span>
              <input value={fieldValues.smtp_username ?? ''} onChange={setField('smtp_username')} />
            </label>
          )}
          {connectionType === 'API_KEY' && (
            <label className="form-field">
              <span>API key *</span>
              <input type="password" value={fieldValues.api_key ?? ''} onChange={setField('api_key')} />
            </label>
          )}
          {connectionType === 'SMTP' && (
            <label className="form-field">
              <span>SMTP password *</span>
              <input type="password" value={fieldValues.smtp_password ?? ''} onChange={setField('smtp_password')} />
            </label>
          )}
          {connectionType === 'OAUTH' && (
            <>
              <label className="form-field">
                <span>Client ID *</span>
                <input type="password" value={fieldValues.client_id ?? ''} onChange={setField('client_id')} />
              </label>
              <label className="form-field">
                <span>Client secret *</span>
                <input type="password" value={fieldValues.client_secret ?? ''} onChange={setField('client_secret')} />
              </label>
              <label className="form-field">
                <span>Tenant ID</span>
                <input value={fieldValues.tenant_id ?? ''} onChange={setField('tenant_id')} />
              </label>
              <p className="health-caption">OAuth flow completes in a later phase. Store client credentials to configure the connection securely.</p>
            </>
          )}
          {error && <div className="form-error" role="alert">{error}</div>}
          <div className="modal-actions">
            <button type="button" className="secondary-button" onClick={onClose}>Cancel</button>
            <button type="submit" className="primary-button" disabled={submit.isPending}>
              {submit.isPending ? 'Connecting...' : 'Create connection'}
            </button>
          </div>
        </form>
      </section>
    </div>
  )
}