import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import OpsCenter from './OpsCenter'

const data = vi.hoisted(() => ({
  overview: {
    readiness: { database: 'ready', redis: 'ready', migrations: 'ready', worker: 'ready', scheduler: 'failed' },
    ready: false,
    metrics: {
      'queue.delivery.due': { __default__: { value: 3 } },
      'db.latency': { __default__: { count: 12, sum: 240, avg: 20, max: 45 } },
    },
    samples: [
      { id: 's1', metric: 'db.latency', value: 21, labels: {}, tenant_id: null, sampled_at: '2026-08-31T10:00:00+00:00' },
    ],
    latest_samples: { 'db.latency': 21, 'queue.delivery.due': 7, 'worker.age': null },
    alerts: {
      open: [
        { id: 'a1', rule: 'scheduler_stopped', severity: 'critical', metric: 'scheduler.age', status: 'OPEN', message: 'Scheduler heartbeat is stale', details: null, tenant_id: null, created_at: '2026-08-31T10:00:00+00:00', acknowledged_at: null, resolved_at: null },
        { id: 'a2', rule: 'disk_storage', severity: 'warning', metric: 'disk.free', status: 'ACKNOWLEDGED', message: 'Low disk space', details: null, tenant_id: null, created_at: '2026-08-31T09:00:00+00:00', acknowledged_at: '2026-08-31T09:10:00+00:00', resolved_at: null },
      ],
      count: 2,
    },
    sampled_at: '2026-08-31T10:05:00+00:00',
  },
}))

vi.mock('../api/ops', () => ({
  opsApi: {
    overview: vi.fn().mockResolvedValue(data.overview),
    evaluate: vi.fn().mockResolvedValue({ results: [], created: 0, updated: 0, resolved: 0 }),
    ack: vi.fn().mockResolvedValue({ alert: data.overview.alerts.open[0] }),
    sample: vi.fn().mockResolvedValue({ values: { 'db.latency': 21 }, sampled_at: '2026-08-31T10:05:00+00:00' }),
  },
}))

function renderOps() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><OpsCenter accessToken="token" /></QueryClientProvider>)
}

describe('OpsCenter', () => {
  it('shows the page heading and system status', async () => {
    renderOps()
    expect(await screen.findByRole('heading', { name: /Ops center/i })).toBeInTheDocument()
    expect(screen.getByText('OPERATIONS')).toBeInTheDocument()
    expect(screen.getByText('System attention needed')).toBeInTheDocument()
  })

  it('renders the readiness chips for every subsystem', async () => {
    renderOps()
    expect(await screen.findByRole('heading', { name: 'Readiness' })).toBeInTheDocument()
    expect(screen.getByText('Database')).toBeInTheDocument()
    expect(screen.getByText('Redis')).toBeInTheDocument()
    expect(screen.getByText('Migrations')).toBeInTheDocument()
    expect(screen.getByText('Worker')).toBeInTheDocument()
    expect(screen.getByText('Scheduler')).toBeInTheDocument()
  })

  it('renders live metric buckets and labels', async () => {
    renderOps()
    expect(await screen.findByRole('heading', { name: 'Live metrics' })).toBeInTheDocument()
    expect(screen.getAllByText('Delivery backlog').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('Database latency').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText(/12 total/)).toBeInTheDocument()
    expect(screen.getByText(/avg 20/)).toBeInTheDocument()
  })

  it('lists open alerts with severity and acknowledge action', async () => {
    renderOps()
    expect(await screen.findByRole('heading', { name: 'Open alerts' })).toBeInTheDocument()
    expect(screen.getByText('Scheduler stopped')).toBeInTheDocument()
    expect(screen.getByText('Scheduler heartbeat is stale')).toBeInTheDocument()
    expect(screen.getByText('critical')).toBeInTheDocument()
    expect(screen.getAllByText('Acknowledge').length).toBe(1)
    expect(screen.getByText('ACKNOWLEDGED')).toBeInTheDocument()
  })

  it('renders the latest samples table', async () => {
    renderOps()
    expect(await screen.findByRole('heading', { name: 'Latest samples' })).toBeInTheDocument()
    expect(screen.getByText('21.00 ms')).toBeInTheDocument()
    expect(screen.getByText('7')).toBeInTheDocument()
  })
})