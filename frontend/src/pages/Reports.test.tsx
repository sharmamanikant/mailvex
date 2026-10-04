import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import Reports from './Reports'

const data = vi.hoisted(() => ({
  dashboard: {
    tenant_id: 'tenant-1',
    window: { range: '', start: null, end: null },
    contacts: { total: 7, active: 3, unsubscribed: 1, suppressed: 1, invalid: 2, inactive: 1, bounced: 1 },
    campaigns: { total: 5, active: 1, scheduled: 2, completed: 1, by_status: { RUNNING: 1, APPROVED: 1, SCHEDULED: 1, COMPLETED: 1, DRAFT: 1 } },
    delivery: { sent: 2, delivered: 1, bounced: 1, unsubscribed: 1, complaints: 1, temporary_failures: 1, blocked: 1, failed: 1 },
    senders: { total: 4, connected: 3, healthy: 2, needs_attention: 1 },
  },
  campaigns: [
    { campaign_id: 'c1', campaign_name: 'Runner', status: 'RUNNING', recipients: 4, sent: 2, delivered: 1, bounced: 1, blocked: 1, unsubscribed: 1, complaints: 1, failed: 1, temporary_failures: 1, delivery_rate: 25 },
  ],
  senders: [
    { sender_id: 's1', sender_name: 'Sender A', sender_email: 'sender-a@example.com', status: 'CONNECTED', health_score: 90, health: 'HEALTHY', messages: 4, successful: 2, failed: 1, temporary_failures: 1, provider_throttling: 1, blocked: 1 },
  ],
}))

vi.mock('../api/reporting', () => ({
  reportingApi: {
    dashboard: vi.fn().mockResolvedValue(data.dashboard),
    campaigns: vi.fn().mockResolvedValue(data.campaigns),
    senders: vi.fn().mockResolvedValue(data.senders),
    contacts: vi.fn().mockResolvedValue(data.dashboard.contacts),
    exportCsv: vi.fn().mockResolvedValue(undefined),
  },
}))

function renderReports() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><Reports accessToken="token" /></QueryClientProvider>)
}

describe('Reports', () => {
  it('shows the business reporting heading and dashboard cards', async () => {
    renderReports()
    expect(await screen.findByRole('heading', { name: /Reports & dashboard/i })).toBeInTheDocument()
    expect(screen.getByText('BUSINESS REPORTING')).toBeInTheDocument()
    expect(screen.getByText('Contacts')).toBeInTheDocument()
    expect(screen.getByText('Campaigns')).toBeInTheDocument()
    expect(screen.getAllByText('Sent').length).toBeGreaterThan(0)
    expect(screen.getByText('Health flags')).toBeInTheDocument()
  })

  it('renders campaign and sender report tables', async () => {
    renderReports()
    expect(await screen.findByText('Campaign report')).toBeInTheDocument()
    expect(screen.getByText('Sender report')).toBeInTheDocument()
    expect(await screen.findByText('Runner')).toBeInTheDocument()
    expect(await screen.findByText('Sender A')).toBeInTheDocument()
    expect(screen.getAllByText('25%').length).toBeGreaterThan(0)
  })

  it('has export CSV buttons for each report', async () => {
    renderReports()
    const exportButtons = await screen.findAllByRole('button', { name: /Export CSV/i })
    expect(exportButtons.length).toBeGreaterThanOrEqual(3)
  })

  it('re-runs the dashboard query when the window filter changes', async () => {
    const user = userEvent.setup()
    renderReports()
    await user.selectOptions(await screen.findByLabelText('Report window'), '7d')
    await waitFor(() => expect(screen.getByLabelText('Report window')).toHaveValue('7d'))
  })

  it('shows date inputs for a custom range', async () => {
    const user = userEvent.setup()
    renderReports()
    await user.selectOptions(await screen.findByLabelText('Report window'), 'custom')
    expect(screen.getByLabelText('Start date')).toBeInTheDocument()
    expect(screen.getByLabelText('End date')).toBeInTheDocument()
  })
})