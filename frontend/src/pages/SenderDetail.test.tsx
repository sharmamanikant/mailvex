import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import SenderDetail from './SenderDetail'

const mocks = vi.hoisted(() => {
  const sender = {
    id: 's-1',
    tenant_id: 't-1',
    mailbox_id: 'box-1',
    provider_connection_id: 'pc-1',
    email: 'alice@example.com',
    display_name: 'Alice Alpha',
    provider: 'GOOGLE',
    status: 'ACTIVE',
    sending_enabled: false,
    availability: {
      available: false,
      sending_enabled: false,
      sender_status: 'ACTIVE',
      mailbox_status: 'SUSPENDED',
      provider_connection_status: 'CONNECTED',
      reason: 'MAILBOX_SUSPENDED',
    },
    mailbox: {
      id: 'box-1',
      email: 'alice@example.com',
      display_name: 'Alice Alpha',
      department: 'Engineering',
      job_title: 'Engineer',
      status: 'SUSPENDED',
      is_suspended: true,
      is_deleted: false,
      last_discovered_at: '2026-09-16T11:00:00Z',
    },
    provider_connection: {
      id: 'pc-1',
      provider: 'GOOGLE',
      status: 'CONNECTED',
      workspace_domain: 'acme.com',
      connection_type: 'OAUTH',
      last_sync_status: 'COMPLETED',
      last_sync_completed_at: '2026-09-16T11:00:00Z',
    },
    health: { status: 'HEALTHY', score: 92, last_checked_at: '2026-09-16T11:00:00Z' },
    created_at: '2026-09-16T10:00:00Z',
    updated_at: '2026-09-16T11:00:00Z',
  }
  const healthOverview = {
    id: 's-1',
    email: 'alice@example.com',
    provider: 'GOOGLE',
    health_status: 'HEALTHY',
    health_score: 92,
    last_health_check_at: '2026-09-16T11:00:00Z',
    latest: {
      health_check_id: 'hc-1',
      sender_id: 's-1',
      tenant_id: 't-1',
      overall_status: 'HEALTHY',
      overall_score: 92,
      score_version: 'v1',
      triggered_by: 'MANUAL',
      started_at: '2026-09-16T11:00:00Z',
      completed_at: '2026-09-16T11:01:00Z',
      duration_ms: 820,
      error_code: null,
      error_message: null,
      results: [
        {
          id: 'r-1',
          check_type: 'PROVIDER_CONNECTION',
          status: 'PASS',
          score: 100,
          severity: 'INFO',
          title: 'Provider connection',
          summary: 'The provider workspace is connected and authorized to send.',
          technical_details: null,
          recommendation: null,
          metadata: {},
          checked_at: '2026-09-16T11:01:00Z',
        },
        {
          id: 'r-2',
          check_type: 'SPF',
          status: 'WARNING',
          score: 60,
          severity: 'MEDIUM',
          title: 'SPF',
          summary: 'Multiple SPF records were found for this domain.',
          technical_details: null,
          recommendation: 'Publish a single SPF record per domain.',
          metadata: {},
          checked_at: '2026-09-16T11:01:00Z',
        },
      ],
      score_explanation: {
        version: 'v1',
        weights: {
          PROVIDER_CONNECTION: 20,
          MAILBOX_STATUS: 15,
          SPF: 15,
          DKIM: 15,
          DMARC: 20,
          DNS: 5,
          SENDING_CONFIGURATION: 5,
          SENDING_SIGNALS: 5,
        },
        unknown_handling:
          'Unknown or not-applicable checks are excluded and their weight is distributed proportionally among the checks that produced a result.',
        thresholds: { healthy: 80, warning: 60 },
      },
    },
    summary: 'Sender configuration health looks good across the checks performed.',
    domain_authentication_summary: [],
  }
  const healthHistory = {
    sender_id: 's-1',
    items: [
      {
        health_check_id: 'hc-1',
        triggered_by: 'SCHEDULED',
        overall_status: 'HEALTHY',
        overall_score: 92,
        score_version: 'v1',
        started_at: '2026-09-16T11:00:00Z',
        completed_at: '2026-09-16T11:01:00Z',
        duration_ms: 820,
        error_code: null,
        result_count: 9,
      },
    ],
    page: 1,
    page_size: 10,
    total: 1,
    total_pages: 1,
  }
  return {
    sender,
    healthOverview,
    healthHistory,
    getDetail: vi.fn(),
    enable: vi.fn(),
    disable: vi.fn(),
    restore: vi.fn(),
    remove: vi.fn(),
    getHealth: vi.fn(),
    getHealthHistory: vi.fn(),
    runHealthCheck: vi.fn(),
  }
})

vi.mock('../api/workspaceSenders', () => ({
  workspaceSendersApi: {
    getDetail: (...args: unknown[]) => mocks.getDetail(...args),
    enable: (...args: unknown[]) => mocks.enable(...args),
    disable: (...args: unknown[]) => mocks.disable(...args),
    restore: (...args: unknown[]) => mocks.restore(...args),
    remove: (...args: unknown[]) => mocks.remove(...args),
    getHealth: (...args: unknown[]) => mocks.getHealth(...args),
    getHealthHistory: (...args: unknown[]) => mocks.getHealthHistory(...args),
    runHealthCheck: (...args: unknown[]) => mocks.runHealthCheck(...args),
  },
}))

mocks.getDetail.mockResolvedValue(mocks.sender)
mocks.enable.mockResolvedValue({ sender_id: 's-1', status: 'ACTIVE', message: 'enabled' })
mocks.disable.mockResolvedValue({ sender_id: 's-1', status: 'ACTIVE', message: 'disabled' })
mocks.restore.mockResolvedValue({ sender_id: 's-1', status: 'ACTIVE', message: 'restored' })
mocks.remove.mockResolvedValue(undefined)
mocks.getHealth.mockResolvedValue(mocks.healthOverview)
mocks.getHealthHistory.mockResolvedValue(mocks.healthHistory)
mocks.runHealthCheck.mockResolvedValue(mocks.healthOverview.latest)

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/senders/workspace/s-1']}>
        <Routes>
          <Route path="/senders/workspace/:id" element={<SenderDetail accessToken="token" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('SenderDetail', () => {
  it('loads and renders the sender overview, mailbox and connection', async () => {
    renderPage()
    expect(await screen.findByRole('heading', { name: 'Alice Alpha' })).toBeInTheDocument()
    expect(screen.getByText('alice@example.com · GOOGLE')).toBeInTheDocument()
    expect(await screen.findByText(/Engineering/i)).toBeInTheDocument()
    expect(screen.getByText('acme.com')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /Provider connection/i })).toBeInTheDocument()
  })

  it('surfaces the availability warning when the sender is not available', async () => {
    renderPage()
    expect(await screen.findByText(/The backing mailbox is suspended/i)).toBeInTheDocument()
  })

  it('enables a disabled sender', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /Enable sending/i }))
    await waitFor(() => expect(mocks.enable).toHaveBeenCalledWith('s-1', 'token'))
    expect(await screen.findByText('Sender enabled.')).toBeInTheDocument()
  })

  it('disables an enabled sender', async () => {
    mocks.getDetail.mockResolvedValueOnce({
      ...mocks.sender,
      sending_enabled: true,
      availability: { ...mocks.sender.availability, available: true, sending_enabled: true, mailbox_status: 'ACTIVE', reason: null },
      mailbox: { ...mocks.sender.mailbox!, status: 'ACTIVE', is_suspended: false },
    })
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /Disable sending/i }))
    await waitFor(() => expect(mocks.disable).toHaveBeenCalledWith('s-1', 'token'))
    expect(await screen.findByText('Sender disabled.')).toBeInTheDocument()
  })

  it('restores a removed sender', async () => {
    mocks.getDetail.mockResolvedValueOnce({
      ...mocks.sender,
      status: 'REMOVED',
      sending_enabled: false,
      availability: { ...mocks.sender.availability, sender_status: 'REMOVED', reason: 'SENDER_REMOVED' },
    })
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /Restore sender/i }))
    await waitFor(() => expect(mocks.restore).toHaveBeenCalledWith('s-1', 'token'))
    expect(await screen.findByText('Sender restored. Re-enable it to start sending again.')).toBeInTheDocument()
  })

  it('removes a sender after inline confirmation', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /Remove sender/i }))
    await user.click(await screen.findByRole('button', { name: /Confirm remove/i }))
    await waitFor(() => expect(mocks.remove).toHaveBeenCalledWith('s-1', 'token'))
    expect(await screen.findByText('Sender removed (soft delete). You can restore it.')).toBeInTheDocument()
  })

  it('shows an error state when the sender does not exist', async () => {
    mocks.getDetail.mockRejectedValueOnce(new Error('Sender not found'))
    renderPage()
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect((await screen.findAllByText('Sender not found')).length).toBeGreaterThan(0)
  })

  it('renders the health overview summary and score', async () => {
    renderPage()
    expect(await screen.findByText(/Sender configuration health looks good/i)).toBeInTheDocument()
    expect(screen.getAllByText('92').length).toBeGreaterThan(0)
  })

  it('runs a health check and shows the completed notice', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /Check health/i }))
    await waitFor(() => expect(mocks.runHealthCheck).toHaveBeenCalledWith('s-1', 'token'))
    expect(await screen.findByText('Health check completed.')).toBeInTheDocument()
  })

  it('shows per-check findings when expanded', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /View findings/i }))
    expect(await screen.findByText(/Multiple SPF records were found/i)).toBeInTheDocument()
    expect(screen.getByText(/Publish a single SPF record per domain/i)).toBeInTheDocument()
    expect(screen.getByText('60 / 100')).toBeInTheDocument()
  })

  it('explains how the score is calculated', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /How is this score calculated/i }))
    expect(await screen.findByText(/excluded and their weight is distributed/i)).toBeInTheDocument()
    expect(screen.getByText(/Healthy ≥ 80/i)).toBeInTheDocument()
    expect(screen.getByText(/Warning ≥ 60/i)).toBeInTheDocument()
  })

  it('shows recent health checks in the history card', async () => {
    renderPage()
    expect(await screen.findByRole('heading', { name: /Health history/i })).toBeInTheDocument()
    expect(await screen.findByText('scheduled')).toBeInTheDocument()
    expect(screen.getByText('9')).toBeInTheDocument()
  })

  it('surfaces the error message when a health check fails', async () => {
    mocks.runHealthCheck.mockRejectedValueOnce(new Error('HEALTH_CHECK_RATE_LIMITED'))
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /Check health/i }))
    expect(await screen.findByText('HEALTH_CHECK_RATE_LIMITED')).toBeInTheDocument()
  })
})