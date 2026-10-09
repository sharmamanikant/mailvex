import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import EmailProviders from './EmailProviders'

const mocks = vi.hoisted(() => {
  const connection = {
    id: 'pc-1',
    provider: 'GOOGLE',
    connection_type: 'OAUTH',
    provider_account_id: '1086989958745',
    workspace_domain: 'acme.com',
    display_name: null,
    status: 'CONNECTED',
    scopes: ['openid', 'userinfo.email', 'admin.directory.user.readonly'],
    credential_configured: true,
    credential_expires_at: '2026-09-17T12:00:00Z',
    connected_by: 'u-1',
    last_sync_at: null,
    provider_metadata: { organizationName: null, defaultDomain: null, microsoftTenantId: null },
    last_sync_stats: {},
    created_at: '2026-09-16T10:00:00Z',
    updated_at: '2026-09-16T10:00:00Z',
  }
  return { connection, startGoogle: vi.fn(), refresh: vi.fn(), redirectTo: vi.fn() }
})

// jsdom cannot perform real navigations, so the OAuth hand-off is stubbed and
// asserted directly instead of throwing a not-implemented error.
vi.mock('../lib/navigation', () => ({
  redirectTo: (...args: unknown[]) => mocks.redirectTo(...args),
}))

vi.mock('../api/providerConnections', () => ({
  providerConnectionsApi: {
    list: vi.fn().mockResolvedValue([mocks.connection]),
    startGoogle: (...args: unknown[]) => mocks.startGoogle(...args),
    disconnect: vi.fn(),
    refresh: (...args: unknown[]) => mocks.refresh(...args),
  },
}))

function renderPage(initialRoute = '/settings/email-providers') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialRoute]}>
        <EmailProviders accessToken="token" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('EmailProviders', () => {
  it('shows the heading and the connected workspace', async () => {
    renderPage()
    expect(screen.getByRole('heading', { name: /Email Providers/i })).toBeInTheDocument()
    expect(await screen.findByText('Google Workspace')).toBeInTheDocument()
    expect(await screen.findByText('acme.com')).toBeInTheDocument()
    expect(screen.getByText('Connected')).toBeInTheDocument()
  })

  it('shows a success notice after an OAuth callback', async () => {
    renderPage('/settings/email-providers?status=connected')
    expect(await screen.findByText(/connected successfully/i)).toBeInTheDocument()
  })

  it('shows a safe error message for an oauth error code', async () => {
    renderPage('/settings/email-providers?status=error&error=access_denied')
    expect(await screen.findByText(/connection cancelled/i)).toBeInTheDocument()
  })

  it('starts google connect and redirects to the provider authorization url', async () => {
    mocks.startGoogle.mockResolvedValue({ authorization_url: 'https://accounts.google.com/o/oauth2/auth?state=x' })
    mocks.redirectTo.mockClear()
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /Connect Google Workspace/i }))
    await waitFor(() => expect(mocks.startGoogle).toHaveBeenCalledWith('token'))
    await waitFor(() => expect(mocks.redirectTo).toHaveBeenCalledWith('https://accounts.google.com/o/oauth2/auth?state=x'))
  })

  it('offers a refresh action for a connected provider', async () => {
    renderPage()
    const refresh = await screen.findByRole('button', { name: /Refresh/i })
    expect(refresh).toBeEnabled()
  })

  it('shows the credential expiry date for a connected provider', async () => {
    renderPage()
    expect(await screen.findByText(/expires/i)).toBeInTheDocument()
  })

  it('refreshes credentials and shows a success notice', async () => {
    mocks.refresh.mockResolvedValue(mocks.connection)
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /Refresh/i }))
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalledWith('pc-1', 'token'))
    expect(await screen.findByText(/refreshed successfully/i)).toBeInTheDocument()
  })

  it('shows an empty state when no provider is linked', async () => {
    const { providerConnectionsApi } = await import('../api/providerConnections')
    vi.mocked(providerConnectionsApi.list).mockResolvedValueOnce([])
    renderPage()
    expect((await screen.findAllByText(/No provider linked/i)).length).toBe(2)
  })
})