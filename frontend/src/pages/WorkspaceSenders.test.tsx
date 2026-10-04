import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import WorkspaceSenders from './WorkspaceSenders'

const mocks = vi.hoisted(() => {
  const availability = (overrides: Partial<{ available: boolean; reason: string | null }>) => ({
    available: true,
    sending_enabled: true,
    sender_status: 'ACTIVE',
    mailbox_status: 'ACTIVE',
    provider_connection_status: 'CONNECTED',
    reason: null,
    ...overrides,
  })
  const alice = {
    id: 's-1',
    tenant_id: 't-1',
    mailbox_id: 'box-1',
    provider_connection_id: 'pc-1',
    email: 'alice@example.com',
    display_name: 'Alice Alpha',
    provider: 'GOOGLE',
    status: 'ACTIVE',
    sending_enabled: true,
    health_status: 'HEALTHY',
    health_score: 92,
    last_health_check_at: '2026-09-16T11:00:00Z',
    availability: availability({}),
    created_at: '2026-09-16T10:00:00Z',
    updated_at: '2026-09-16T11:00:00Z',
  }
  const bob = {
    id: 's-2',
    tenant_id: 't-1',
    mailbox_id: 'box-2',
    provider_connection_id: 'pc-1',
    email: 'bob@example.com',
    display_name: 'Bob Beta',
    provider: 'GOOGLE',
    status: 'ACTIVE',
    sending_enabled: false,
    health_status: 'UNKNOWN',
    health_score: null,
    last_health_check_at: null,
    availability: availability({ available: false, reason: 'MAILBOX_SUSPENDED' }),
    created_at: '2026-09-16T10:00:00Z',
    updated_at: '2026-09-16T11:00:00Z',
  }
  return {
    alice,
    bob,
    list: vi.fn(),
    enable: vi.fn(),
    disable: vi.fn(),
  }
})

vi.mock('../api/workspaceSenders', () => ({
  workspaceSendersApi: {
    list: (...args: unknown[]) => mocks.list(...args),
    enable: (...args: unknown[]) => mocks.enable(...args),
    disable: (...args: unknown[]) => mocks.disable(...args),
  },
}))

mocks.list.mockResolvedValue({
  items: [mocks.alice, mocks.bob],
  page: 1,
  page_size: 25,
  total: 2,
  total_pages: 1,
})
mocks.enable.mockResolvedValue({ sender_id: 's-2', status: 'ACTIVE', message: 'enabled' })
mocks.disable.mockResolvedValue({ sender_id: 's-1', status: 'ACTIVE', message: 'disabled' })

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/senders/workspace']}>
        <WorkspaceSenders accessToken="token" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('WorkspaceSenders', () => {
  it('shows the heading and sender rows with availability', async () => {
    renderPage()
    expect(screen.getByRole('heading', { name: /Senders/i })).toBeInTheDocument()
    expect(await screen.findByText(/Alice Alpha/i)).toBeInTheDocument()
    expect(screen.getByText('alice@example.com')).toBeInTheDocument()
    expect(screen.getByText(/bob@example.com/i)).toBeInTheDocument()
    expect(await screen.findByText('Available')).toBeInTheDocument()
    expect(screen.getByText('Mailbox suspended')).toBeInTheDocument()
  })

  it('requests the default page parameters', async () => {
    renderPage()
    await waitFor(() => {
      expect(mocks.list).toHaveBeenCalledWith(expect.anything(), 'token')
    })
    const params = mocks.list.mock.calls[0][0] as URLSearchParams
    expect(params.get('page')).toBe('1')
    expect(params.get('page_size')).toBe('25')
    expect(params.get('sort')).toBe('-created_at')
  })

  it('searches senders by email or display name', async () => {
    const user = userEvent.setup()
    renderPage()
    const search = await screen.findByLabelText(/Search senders/i)
    await user.type(search, 'acme')
    await waitFor(() => {
      const lastCall = mocks.list.mock.calls[mocks.list.mock.calls.length - 1]
      expect((lastCall[0] as URLSearchParams).get('search')).toBe('acme')
    })
  })

  it('filters senders by status and sending state', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.selectOptions(await screen.findByLabelText(/Sender status/i), 'ACTIVE')
    await waitFor(() => {
      const lastCall = mocks.list.mock.calls[mocks.list.mock.calls.length - 1]
      expect((lastCall[0] as URLSearchParams).get('status')).toBe('ACTIVE')
    })
    await user.selectOptions(await screen.findByLabelText(/Sending state/i), 'true')
    await waitFor(() => {
      const lastCall = mocks.list.mock.calls[mocks.list.mock.calls.length - 1]
      expect((lastCall[0] as URLSearchParams).get('sending')).toBe('true')
    })
  })

  it('filters senders by provider and health', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.selectOptions(await screen.findByLabelText(/^Provider/i), 'MICROSOFT')
    await waitFor(() => {
      const lastCall = mocks.list.mock.calls[mocks.list.mock.calls.length - 1]
      expect((lastCall[0] as URLSearchParams).get('provider')).toBe('MICROSOFT')
    })
    await user.selectOptions(await screen.findByLabelText(/Health status/i), 'HEALTHY')
    await waitFor(() => {
      const lastCall = mocks.list.mock.calls[mocks.list.mock.calls.length - 1]
      expect((lastCall[0] as URLSearchParams).get('health_status')).toBe('HEALTHY')
    })
  })

  it('paginates to the next page', async () => {
    mocks.list.mockResolvedValueOnce({
      items: [mocks.alice],
      page: 1,
      page_size: 25,
      total: 60,
      total_pages: 3,
    })
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /Next page/i }))
    await waitFor(() => {
      const lastCall = mocks.list.mock.calls[mocks.list.mock.calls.length - 1]
      expect((lastCall[0] as URLSearchParams).get('page')).toBe('2')
    })
  })

  it('enables a disabled sender from the row', async () => {
    const user = userEvent.setup()
    renderPage()
    const row = (await screen.findByText(/bob@example.com/i)).closest('tr') as HTMLElement
    await user.click(within(row).getByRole('button', { name: /Enable/i }))
    await waitFor(() => expect(mocks.enable).toHaveBeenCalledWith('s-2', 'token'))
    expect(await screen.findByText('Sender enabled.')).toBeInTheDocument()
  })

  it('disables an enabled sender from the row', async () => {
    const user = userEvent.setup()
    renderPage()
    const row = (await screen.findByText('Alice Alpha')).closest('tr') as HTMLElement
    await user.click(within(row).getByRole('button', { name: /Disable/i }))
    await waitFor(() => expect(mocks.disable).toHaveBeenCalledWith('s-1', 'token'))
    expect(await screen.findByText('Sender disabled.')).toBeInTheDocument()
  })

  it('shows the health status and score in the health column', async () => {
    renderPage()
    const aliceRow = (await screen.findByText('Alice Alpha')).closest('tr') as HTMLElement
    expect(within(aliceRow).getByText('Healthy')).toBeInTheDocument()
    expect(within(aliceRow).getByText('92')).toBeInTheDocument()
    const bobRow = (await screen.findByText('Bob Beta')).closest('tr') as HTMLElement
    expect(within(bobRow).getByText('Unknown')).toBeInTheDocument()
  })

  it('shows an empty state when no senders match', async () => {
    mocks.list.mockResolvedValueOnce({ items: [], page: 1, page_size: 25, total: 0, total_pages: 0 })
    renderPage()
    expect(await screen.findByText('No senders yet')).toBeInTheDocument()
  })
})