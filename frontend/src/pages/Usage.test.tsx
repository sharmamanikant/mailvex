import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import Usage from './Usage'

const data = vi.hoisted(() => ({
  overview: {
    tenant_id: 'tenant-1',
    plan_code: 'free',
    plan_name: 'Free',
    price_usd_mo: 0,
    features: ['Core outreach'],
    status: 'ACTIVE',
    period_start: '2026-08-01T00:00:00+00:00',
    period_end: '2026-08-31T23:59:59+00:00',
    metrics: [
      { metric: 'contacts', label: 'Contacts', used: 750, limit: 1000, remaining: 250, scope: 'absolute' },
      { metric: 'ai_generations', label: 'AI generations', used: 45, limit: 200, remaining: 155, scope: 'period' },
      { metric: 'storage_mb', label: 'Storage', used: 2100, limit: 5120, remaining: 3020, scope: 'absolute' },
    ],
  },
  plans: [
    { code: 'free', name: 'Free', price_usd_mo: 0, features: ['Core outreach'], limits: [{ metric: 'contacts', label: 'Contacts', limit: 1000, scope: 'absolute' }] },
    { code: 'enterprise', name: 'Enterprise', price_usd_mo: 499, features: ['Unlimited everything'], limits: [{ metric: 'contacts', label: 'Contacts', limit: null, scope: 'absolute' }] },
  ],
  events: [
    { id: 'evt-1', event_type: 'CONTACT_CREATED', period_start: '2026-08-01T00:00:00+00:00', quantity: 1, resource_type: 'contact', resource_id: 'c1', metadata: {}, created_at: '2026-08-10T12:00:00+00:00' },
  ],
}))

vi.mock('../api/billing', () => ({
  usageApi: {
    overview: vi.fn().mockResolvedValue(data.overview),
    plans: vi.fn().mockResolvedValue(data.plans),
    events: vi.fn().mockResolvedValue(data.events),
  },
}))

function renderUsage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><Usage accessToken="token" /></QueryClientProvider>)
}

describe('Usage', () => {
  it('shows the usage heading and current plan', async () => {
    renderUsage()
    expect(await screen.findByRole('heading', { name: /Usage & plan/i })).toBeInTheDocument()
    expect(screen.getByText('USAGE & BILLING')).toBeInTheDocument()
    expect(screen.getAllByText('Free').length).toBeGreaterThan(0)
    expect(screen.getByText('CURRENT PLAN')).toBeInTheDocument()
  })

  it('renders per-metric meters with used, limit, and progress', async () => {
    renderUsage()
    expect((await screen.findAllByText('Contacts')).length).toBeGreaterThan(0)
    expect(screen.getByText('AI generations')).toBeInTheDocument()
    expect(screen.getByText('Storage')).toBeInTheDocument()
    expect(screen.getAllByText(/1,000/).length).toBeGreaterThan(0)
    expect(screen.getByText('2.1 GB')).toBeInTheDocument()
    expect(screen.getAllByText(/5\.0 GB/).length).toBeGreaterThan(0)
  })

  it('lists available plans with pricing', async () => {
    renderUsage()
    expect(await screen.findByRole('heading', { name: 'Available plans' })).toBeInTheDocument()
    expect(screen.getByText('$0/mo')).toBeInTheDocument()
    expect(screen.getByText('$499/mo')).toBeInTheDocument()
  })

  it('renders recent usage activity', async () => {
    renderUsage()
    expect(await screen.findByRole('heading', { name: 'Recent activity' })).toBeInTheDocument()
    expect(screen.getByText('CONTACT_CREATED')).toBeInTheDocument()
    expect(screen.getByText('contact')).toBeInTheDocument()
  })
})