import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import Dashboard from './Dashboard'
import type { User } from '../types/auth'

function user(overrides: Partial<User> = {}): User {
  return {
    id: 'u-1',
    tenant_id: 't-1',
    email: 'user@acme.com',
    display_name: 'Ada Lovelace',
    roles: ['MEMBER'],
    permissions: [],
    ...overrides,
  }
}

function renderShell(current: User) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Dashboard user={current} onLogout={vi.fn()}>
          <main>page body</main>
        </Dashboard>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('Dashboard navigation', () => {
  it('hides platform owner tooling from ordinary members', () => {
    renderShell(user())
    expect(screen.queryByText('Platform Owner')).not.toBeInTheDocument()
  })

  it('hides admin and operations links without settings.manage', () => {
    renderShell(user({ roles: ['MEMBER'], permissions: ['contacts.read'] }))
    expect(screen.queryByText('Workspace Admin')).not.toBeInTheDocument()
    expect(screen.queryByText('Operations')).not.toBeInTheDocument()
  })

  it('shows admin and operations links once settings.manage is granted', () => {
    renderShell(user({ permissions: ['settings.manage'] }))
    expect(screen.getByText('Workspace Admin')).toBeInTheDocument()
    expect(screen.getByText('Operations')).toBeInTheDocument()
    expect(screen.queryByText('Platform Owner')).not.toBeInTheDocument()
  })

  it('only shows platform owner tooling for super admins', () => {
    renderShell(user({ roles: ['SUPER_ADMIN'] }))
    expect(screen.getByText('Platform Owner')).toBeInTheDocument()
  })

  it('honours the wildcard grant used for privileged roles', () => {
    renderShell(user({ roles: ['OWNER'] }))
    expect(screen.getByText('Workspace Admin')).toBeInTheDocument()
    expect(screen.queryByText('Platform Owner')).not.toBeInTheDocument()
  })

  it('exposes sender health through the email accounts rows, not a duplicate nav link', () => {
    renderShell(user())
    expect(screen.getByText('Email Accounts')).toBeInTheDocument()
    expect(screen.queryByText('Sender Health')).not.toBeInTheDocument()
  })
})