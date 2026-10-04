import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import Contacts from './Contacts'

const data = vi.hoisted(() => ({
  contacts: [
    {
      id: 'c1', tenant_id: 't1', first_name: 'Ada', last_name: 'Lovelace', email: 'ada@acme.com', phone: '+442071838750',
      company: 'Acme', designation: 'CTO', location: 'London', website: null, industry: null, source: 'import', source_reference: null,
      status: 'ACTIVE', validation_status: 'VALID', suppression_status: 'CLEAR', unsubscribe_status: 'SUBSCRIBED',
      custom_fields: {}, tags: [], list_ids: [],
      email_status: 'VALID', email_type: 'BUSINESS', email_provider: 'Google Workspace', domain_status: 'EXISTS', mx_status: 'VALID',
      smtp_status: 'ACCEPTED', disposable: false, role_account: false, catch_all: false,
      phone_status: 'VALID', phone_type: 'FIXED_LINE', company_status: 'MATCH', duplicate_status: 'UNIQUE', duplicate_score: null,
      verification_score: 96, risk_level: 'LOW', verification_status: 'VERIFIED', last_verified_at: '2026-09-25T10:00:00+00:00',
      verification_details: null, created_at: '2026-09-01T10:00:00+00:00', updated_at: '2026-09-25T10:00:00+00:00',
    },
    {
      id: 'c2', tenant_id: 't1', first_name: 'Support', last_name: null, email: 'info@gmail.com', phone: null,
      company: null, designation: null, location: null, website: null, industry: null, source: 'manual', source_reference: null,
      status: 'ACTIVE', validation_status: 'VALID', suppression_status: 'CLEAR', unsubscribe_status: 'SUBSCRIBED',
      custom_fields: {}, tags: [], list_ids: [],
      email_status: 'VALID', email_type: 'FREE_MAILBOX', email_provider: 'Gmail', domain_status: 'EXISTS', mx_status: 'VALID',
      smtp_status: 'NOT_PROBED', disposable: false, role_account: true, catch_all: null,
      phone_status: 'NOT_PROVIDED', phone_type: 'UNKNOWN', company_status: 'UNKNOWN', duplicate_status: 'UNIQUE', duplicate_score: null,
      verification_score: 55, risk_level: 'MEDIUM', verification_status: 'NEEDS_REVIEW', last_verified_at: '2026-09-25T10:05:00+00:00',
      verification_details: null, created_at: '2026-09-02T10:00:00+00:00', updated_at: '2026-09-25T10:05:00+00:00',
    },
    {
      id: 'c3', tenant_id: 't1', first_name: null, last_name: null, email: 'bounce@mailinator.com', phone: null,
      company: null, designation: null, location: null, website: null, industry: null, source: 'import', source_reference: null,
      status: 'ACTIVE', validation_status: 'VALID', suppression_status: 'CLEAR', unsubscribe_status: 'SUBSCRIBED',
      custom_fields: {}, tags: [], list_ids: [],
      email_status: 'VALID', email_type: 'DISPOSABLE', email_provider: 'Mailinator', domain_status: 'EXISTS', mx_status: 'VALID',
      smtp_status: 'NOT_PROBED', disposable: true, role_account: false, catch_all: null,
      phone_status: 'NOT_PROVIDED', phone_type: 'UNKNOWN', company_status: 'UNKNOWN', duplicate_status: 'DUPLICATE', duplicate_score: 0.91,
      verification_score: 10, risk_level: 'HIGH', verification_status: 'INVALID', last_verified_at: '2026-09-25T10:10:00+00:00',
      verification_details: null, created_at: '2026-09-03T10:00:00+00:00', updated_at: '2026-09-25T10:10:00+00:00',
    },
    {
      id: 'c4', tenant_id: 't1', first_name: 'Never', last_name: 'Checked', email: 'new@acme.com', phone: null,
      company: 'Acme', designation: null, location: null, website: null, industry: null, source: 'manual', source_reference: null,
      status: 'ACTIVE', validation_status: 'VALID', suppression_status: 'CLEAR', unsubscribe_status: 'SUBSCRIBED',
      custom_fields: {}, tags: [], list_ids: [],
      email_status: 'UNKNOWN', email_type: 'UNKNOWN', email_provider: 'Unknown', domain_status: 'UNKNOWN', mx_status: 'UNKNOWN',
      smtp_status: 'NOT_PROBED', disposable: false, role_account: false, catch_all: null,
      phone_status: 'NOT_PROVIDED', phone_type: 'UNKNOWN', company_status: 'UNKNOWN', duplicate_status: 'UNKNOWN', duplicate_score: null,
      verification_score: null, risk_level: 'UNKNOWN', verification_status: 'NOT_VERIFIED', last_verified_at: null,
      verification_details: null, created_at: '2026-09-04T10:00:00+00:00', updated_at: '2026-09-04T10:00:00+00:00',
    },
  ],
  summary: { by_status: { VERIFIED: 1, NEEDS_REVIEW: 1, INVALID: 1, NOT_VERIFIED: 1 }, by_risk: { LOW: 1, MEDIUM: 1, HIGH: 1, UNKNOWN: 1 }, total: 4 },
  job: {
    id: 'job1', status: 'RUNNING', scope: 'selected', total_count: 4, processed_count: 2, valid_count: 1, invalid_count: 0,
    risky_count: 0, needs_review_count: 1, duplicate_count: 0, unknown_count: 0, failed_count: 0, error_message: null,
    started_at: '2026-09-25T10:00:00+00:00', finished_at: null, created_at: '2026-09-25T10:00:00+00:00',
  },
  listParams: [] as string[],
  bulkVerifyCalls: [] as unknown[],
}))

vi.mock('../api/contacts', () => ({
  contactsApi: {
    list: vi.fn((params: URLSearchParams) => {
      data.listParams.push(params.toString())
      return Promise.resolve({ items: data.contacts, page: 1, page_size: 25, total: data.contacts.length })
    }),
    lists: vi.fn().mockResolvedValue([]),
    tags: vi.fn().mockResolvedValue([]),
    verificationSummary: vi.fn().mockResolvedValue(data.summary),
    verificationJob: vi.fn().mockResolvedValue(data.job),
    cancelVerificationJob: vi.fn().mockResolvedValue({ ...data.job, status: 'CANCELLED' }),
    bulkVerify: vi.fn((input: unknown) => {
      data.bulkVerifyCalls.push(input)
      return Promise.resolve(data.job)
    }),
    verifyContact: vi.fn().mockResolvedValue({ contact_id: 'c1', job_id: 'job1', status: 'QUEUED', probe_smtp: false }),
    remove: vi.fn().mockResolvedValue(undefined),
    bulk: vi.fn().mockResolvedValue({ affected: 0, skipped: 0, failed: 0 }),
  },
}))

function renderContacts() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter><Contacts accessToken="token" /></MemoryRouter></QueryClientProvider>)
}

describe('Contacts validation UI', () => {
  beforeEach(() => {
    data.listParams.length = 0
    data.bulkVerifyCalls.length = 0
  })

  it('renders a verification column with a verdict badge per contact', async () => {
    renderContacts()
    expect(await screen.findByRole('columnheader', { name: 'Verification' })).toBeInTheDocument()
    const table = within(screen.getByRole('table'))
    expect(table.getByText('VERIFIED')).toBeInTheDocument()
    expect(table.getByText('NEEDS REVIEW')).toBeInTheDocument()
    expect(table.getByText('INVALID')).toBeInTheDocument()
  })

  it('does not treat a free mailbox or a role account as invalid', async () => {
    renderContacts()
    const checkbox = await screen.findByLabelText('Select info@gmail.com')
    const row = checkbox.closest('tr') as HTMLElement
    expect(row.textContent).toContain('NEEDS REVIEW')
    expect(row.textContent).not.toContain('INVALID')
    expect(row.textContent).toContain('free mailbox')
    expect(row.textContent).toContain('role account')
  })

  it('shows unverified contacts without a verdict badge', async () => {
    renderContacts()
    await screen.findByText('new@acme.com')
    const row = screen.getByText('new@acme.com').closest('tr') as HTMLElement
    expect(row.textContent).toContain('Not verified')
  })

  it('passes the verification status and risk filters to the API', async () => {
    const user = userEvent.setup()
    renderContacts()
    await screen.findByRole('columnheader', { name: 'Verification' })
    await user.selectOptions(screen.getByLabelText('Filter by verification status'), 'RISKY')
    await waitFor(() => {
      expect(data.listParams.some((raw) => raw.includes('verification_status=RISKY'))).toBe(true)
    })
    await user.selectOptions(screen.getByLabelText('Filter by risk level'), 'HIGH')
    await waitFor(() => {
      expect(data.listParams.some((raw) => raw.includes('risk_level=HIGH'))).toBe(true)
    })
  })

  it('treats a validation filter as a filter for the empty state and clear button', async () => {
    const user = userEvent.setup()
    renderContacts()
    await screen.findByRole('columnheader', { name: 'Verification' })
    await user.selectOptions(screen.getByLabelText('Filter by risk level'), 'HIGH')
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Clear' }).length).toBeGreaterThanOrEqual(1))
    const clear = screen.getAllByRole('button', { name: 'Clear' })[0]
    await user.click(clear)
    await waitFor(() => {
      expect((screen.getByLabelText('Filter by risk level') as HTMLSelectElement).value).toBe('')
    })
  })

  it('queues a bulk verification for the selected contacts', async () => {
    const user = userEvent.setup()
    renderContacts()
    await screen.findByText('ada@acme.com')
    await user.click(screen.getByLabelText('Select ada@acme.com'))
    await user.click(screen.getByRole('button', { name: /Verify 1/ }))
    await waitFor(() => {
      expect(data.bulkVerifyCalls).toHaveLength(1)
      expect(data.bulkVerifyCalls[0]).toEqual({ contact_ids: ['c1'], probe_smtp: false })
    })
  })

  it('forwards the SMTP probe toggle to the bulk request', async () => {
    const user = userEvent.setup()
    renderContacts()
    await screen.findByText('ada@acme.com')
    await user.click(screen.getByLabelText('Select ada@acme.com'))
    await user.click(screen.getByLabelText('Probe SMTP'))
    await user.click(screen.getByRole('button', { name: /Verify 1/ }))
    await waitFor(() => {
      expect(data.bulkVerifyCalls[0]).toEqual({ contact_ids: ['c1'], probe_smtp: true })
    })
  })

  it('runs a whole-directory verification with an explicit all-contacts scope', async () => {
    const user = userEvent.setup()
    renderContacts()
    await screen.findByRole('columnheader', { name: 'Verification' })
    await user.click(screen.getByRole('button', { name: /Verify all/ }))
    await waitFor(() => {
      expect(data.bulkVerifyCalls[0]).toEqual({ all_contacts: true, probe_smtp: false })
    })
  })

  it('shows live progress and the verdict breakdown for the active job', async () => {
    const user = userEvent.setup()
    renderContacts()
    await screen.findByRole('columnheader', { name: 'Verification' })
    await user.click(screen.getByRole('button', { name: /Verify all/ }))
    expect(await screen.findByText('RUNNING')).toBeInTheDocument()
    expect(screen.getByText('2 / 4 (50%)')).toBeInTheDocument()
    expect(screen.getByText('1 valid')).toBeInTheDocument()
    expect(screen.getByText('1 review')).toBeInTheDocument()
  })
})
