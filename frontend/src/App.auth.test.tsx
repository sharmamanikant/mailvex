import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const mocks = vi.hoisted(() => ({
  me: vi.fn(),
  refresh: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('./api/client', () => ({
  authApi: {
    me: (...args: unknown[]) => mocks.me(...args),
    refresh: () => mocks.refresh(),
    logout: () => mocks.logout(),
  },
}))

const user = {
  id: 'u-1',
  tenant_id: 't-1',
  email: 'ada@example.com',
  display_name: 'Ada Admin',
  roles: ['Admin'],
}

function makeToken(expiresInSeconds: number) {
  const encode = (value: object) => btoa(JSON.stringify(value)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  const header = encode({ alg: 'HS256', typ: 'JWT' })
  const claims = encode({ sub: 'u-1', tenant_id: 't-1', type: 'access', exp: Math.floor(Date.now() / 1000) + expiresInSeconds })
  return `${header}.${claims}.signature`
}

function issued(token: string) {
  return { access_token: token, token_type: 'bearer', expires_at: new Date(Date.now() + 900_000).toISOString() }
}

function renderApp(route = '/dashboard') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('App access token lifecycle', () => {
  beforeEach(() => {
    localStorage.clear()
    mocks.me.mockReset()
    mocks.refresh.mockReset()
    mocks.logout.mockReset()
    mocks.logout.mockResolvedValue(undefined)
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
  })

  it('recovers from an expired access token using the refresh cookie', async () => {
    const stale = makeToken(-60)
    const fresh = makeToken(900)
    localStorage.setItem('crcrm_access_token', stale)
    mocks.me.mockImplementation((token: string) => token === stale
      ? Promise.reject(new Error('Invalid authentication token'))
      : Promise.resolve(user))
    mocks.refresh.mockResolvedValue(issued(fresh))

    renderApp()

    expect(await screen.findByRole('link', { name: /All Contacts/i })).toBeInTheDocument()
    expect(mocks.refresh).toHaveBeenCalled()
    expect(mocks.me).toHaveBeenCalledWith(fresh)
    await waitFor(() => expect(localStorage.getItem('crcrm_access_token')).toBe(fresh))
  })

  it('renews a token that is still valid but close to expiry', async () => {
    const current = makeToken(30)
    const renewed = makeToken(900)
    localStorage.setItem('crcrm_access_token', current)
    mocks.me.mockResolvedValue(user)
    mocks.refresh.mockResolvedValue(issued(renewed))

    renderApp()

    expect(await screen.findByRole('link', { name: /All Contacts/i })).toBeInTheDocument()
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalled())
    await waitFor(() => expect(localStorage.getItem('crcrm_access_token')).toBe(renewed))
  })

  it('asks for a new sign-in when the refresh token is rejected', async () => {
    localStorage.setItem('crcrm_access_token', makeToken(-60))
    mocks.me.mockRejectedValue(new Error('Invalid authentication token'))
    mocks.refresh.mockRejectedValue(new Error('Invalid refresh token'))

    renderApp('/contacts/import')

    expect(await screen.findByRole('button', { name: /Sign in/i })).toBeInTheDocument()
    expect(localStorage.getItem('crcrm_access_token')).toBeNull()
  })
})
