import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import WorkspaceMailboxes from './WorkspaceMailboxes'

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
    last_sync_at: '2026-09-16T11:00:00Z',
    last_sync_status: 'COMPLETED',
    last_sync_error: null,
    last_sync_started_at: '2026-09-16T10:59:00Z',
    last_sync_completed_at: '2026-09-16T11:00:00Z',
    provider_metadata: { organizationName: null, defaultDomain: null, microsoftTenantId: null },
    last_sync_stats: { created: 3, updated: 0, suspended: 1, deleted: 0, skipped: 0 },
    created_at: '2026-09-16T10:00:00Z',
    updated_at: '2026-09-16T11:00:00Z',
  }
  const mailboxes = [
    {
      id: 'box-1',
      provider_connection_id: 'pc-1',
      provider_mailbox_id: 'u-1',
      email: 'alice@example.com',
      display_name: 'Alice Alpha',
      first_name: 'Alice',
      last_name: 'Alpha',
      department: 'Engineering',
      job_title: 'Engineer',
      user_type: 'USER',
      provider_status: 'ACTIVE',
      is_suspended: false,
      is_deleted: false,
      last_discovered_at: '2026-09-16T11:00:00Z',
      created_at: '2026-09-16T11:00:00Z',
      updated_at: '2026-09-16T11:00:00Z',
    },
    {
      id: 'box-2',
      provider_connection_id: 'pc-1',
      provider_mailbox_id: 'u-2',
      email: 'bob@example.com',
      display_name: 'Bob Beta',
      first_name: 'Bob',
      last_name: 'Beta',
      department: 'Engineering',
      job_title: 'Engineer',
      user_type: 'USER',
      provider_status: 'ACTIVE',
      is_suspended: false,
      is_deleted: false,
      last_discovered_at: '2026-09-16T11:00:00Z',
      created_at: '2026-09-16T11:00:00Z',
      updated_at: '2026-09-16T11:00:00Z',
    },
    {
      id: 'box-3',
      provider_connection_id: 'pc-1',
      provider_mailbox_id: 'u-3',
      email: 'carol@example.com',
      display_name: 'Carol',
      first_name: 'Carol',
      last_name: null,
      department: 'Sales',
      job_title: 'Rep',
      user_type: 'USER',
      provider_status: 'SUSPENDED',
      is_suspended: true,
      is_deleted: false,
      last_discovered_at: '2026-09-16T11:00:00Z',
      created_at: '2026-09-16T11:00:00Z',
      updated_at: '2026-09-16T11:00:00Z',
    },
  ]
  return {
    connection,
    mailboxes,
    listConnections: vi.fn(),
    listMailboxes: vi.fn(),
    sync: vi.fn(),
  }
})

vi.mock('../api/providerConnections', () => ({
  providerConnectionsApi: {
    list: (...args: unknown[]) => mocks.listConnections(...args),
  },
}))

vi.mock('../api/workspaceMailboxes', () => ({
  workspaceMailboxesApi: {
    sync: (...args: unknown[]) => mocks.sync(...args),
    list: (...args: unknown[]) => mocks.listMailboxes(...args),
    get: vi.fn(),
  },
}))

mocks.listConnections.mockResolvedValue([mocks.connection])
mocks.listMailboxes.mockResolvedValue({
  items: mocks.mailboxes,
  page: 1,
  page_size: 25,
  total: 3,
})
mocks.sync.mockResolvedValue(mocks.connection)

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/settings/email-providers/mailboxes']}>
        <WorkspaceMailboxes accessToken="token" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('WorkspaceMailboxes', () => {
  it('shows the heading, workspace, sync status and mailbox rows', async () => {
    renderPage()
    expect(screen.getByRole('heading', { name: /Workspace Mailboxes/i })).toBeInTheDocument()
    expect(await screen.findByText('acme.com')).toBeInTheDocument()
    expect(await screen.findByText(/Alice Alpha/i)).toBeInTheDocument()
    expect(screen.getByText(/bob@example.com/i)).toBeInTheDocument()
    expect(await screen.findByText('Completed')).toBeInTheDocument()
    expect(await screen.findByText(/last synced/i)).toBeInTheDocument()
  })

  it('lists mailboxes for the selected workspace', async () => {
    renderPage()
    await waitFor(() => {
      expect(mocks.listMailboxes).toHaveBeenCalledWith('pc-1', expect.anything(), 'token')
    })
    const params = mocks.listMailboxes.mock.calls[0][1] as URLSearchParams
    expect(params.get('page')).toBe('1')
    expect(params.get('page_size')).toBe('25')
  })

  it('runs a sync for the selected workspace', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /Sync mailboxes/i }))
    await waitFor(() => expect(mocks.sync).toHaveBeenCalledWith('pc-1', 'token'))
    expect(await screen.findByText(/synchronization started/i)).toBeInTheDocument()
  })

  it('searches mailboxes by name/email/department', async () => {
    const user = userEvent.setup()
    renderPage()
    const search = await screen.findByLabelText(/Search mailboxes/i)
    await user.type(search, 'sales')
    await waitFor(() => {
      expect(mocks.listMailboxes).toHaveBeenLastCalledWith('pc-1', expect.anything(), 'token')
    })
    const lastCall = mocks.listMailboxes.mock.calls[mocks.listMailboxes.mock.calls.length - 1]
    const params = lastCall[1] as URLSearchParams
    expect(params.get('search')).toBe('sales')
  })

  it('filters mailboxes by status', async () => {
    const user = userEvent.setup()
    renderPage()
    const filter = await screen.findByLabelText(/Mailbox status/i)
    await user.selectOptions(filter, 'SUSPENDED')
    await waitFor(() => {
      const lastCall = mocks.listMailboxes.mock.calls[mocks.listMailboxes.mock.calls.length - 1]
      expect((lastCall[1] as URLSearchParams).get('status')).toBe('SUSPENDED')
    })
  })

  it('paginates to the next page', async () => {
    mocks.listMailboxes.mockResolvedValueOnce({
      items: [mocks.mailboxes[0]],
      page: 1,
      page_size: 25,
      total: 76,
    })
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /Next page/i }))
    await waitFor(() => {
      const lastCall = mocks.listMailboxes.mock.calls[mocks.listMailboxes.mock.calls.length - 1]
      expect((lastCall[1] as URLSearchParams).get('page')).toBe('2')
    })
  })

  it('selects a single mailbox and shows the selection count', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByLabelText('Select alice@example.com'))
    expect(await screen.findByText('1 selected')).toBeInTheDocument()
  })

  it('selects all visible mailboxes', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByLabelText('Select all visible'))
    expect(await screen.findByText('3 selected')).toBeInTheDocument()
  })

  it('shows an empty state when no workspace is connected', async () => {
    mocks.listConnections.mockResolvedValueOnce([])
    renderPage()
    expect(await screen.findByText(/No provider connected/i)).toBeInTheDocument()
    mocks.listConnections.mockResolvedValue([mocks.connection])
  })

  it('shows an empty state when a sync has discovered no mailboxes', async () => {
    mocks.listMailboxes.mockResolvedValueOnce({ items: [], page: 1, page_size: 25, total: 0 })
    renderPage()
    expect(await screen.findByText('No mailboxes found')).toBeInTheDocument()
  })
})