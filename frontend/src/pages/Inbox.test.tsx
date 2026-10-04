import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import Inbox from './Inbox'

const data = vi.hoisted(() => ({
  thread: {
    id: 'thread-1',
    sender_id: 'sender-1',
    sender_email: 'sender@example.com',
    external_thread_id: 'ext-1',
    subject: 'Re: Hello',
    last_message_at: '2026-08-25T10:00:00Z',
    status: 'UNREAD',
    match_status: 'UNMATCHED',
    provider: 'GOOGLE',
    campaign_id: null,
    contact_id: null,
    contact_name: 'Alice',
    snippet: 'Interested, tell me more',
  },
  detail: {
    id: 'thread-1',
    sender_id: 'sender-1',
    sender_email: 'sender@example.com',
    external_thread_id: 'ext-1',
    subject: 'Re: Hello',
    last_message_at: '2026-08-25T10:00:00Z',
    status: 'UNREAD',
    match_status: 'UNMATCHED',
    provider: 'GOOGLE',
    campaign_id: null,
    contact_id: 'c1',
    contact_name: 'Alice',
    snippet: 'Interested, tell me more',
    campaign_name: null,
    messages: [{ id: 'm1', external_message_id: 'm1', direction: 'INBOUND', from_email: 'alice@example.com', to_email: 'sender@example.com', subject: 'Re: Hello', body_text: 'Interested!', received_at: '2026-08-25T10:00:00Z', status: 'RECEIVED' }],
    recipient: { contact_id: 'c1', name: 'Alice Smith', email: 'alice@example.com', company: 'Acme', designation: 'CTO', campaign_name: null, campaign_id: null, last_contact: null },
  },
}))

vi.mock('../api/inbox', () => ({
  inboxApi: {
    listThreads: vi.fn().mockResolvedValue({ items: [data.thread], total: 1, page: 1, page_size: 50 }),
    getThread: vi.fn().mockResolvedValue(data.detail),
    setStatus: vi.fn(),
    sync: vi.fn(),
    draft: vi.fn().mockResolvedValue({ draft_id: 'd1', thread_id: data.thread.id, subject: 'Re: Hello', body: 'Drafted reply', status: 'DRAFT', provider: 'MOCK' }),
    approve: vi.fn().mockResolvedValue({ draft_id: 'd1', thread_id: data.thread.id, status: 'APPROVED' }),
    send: vi.fn().mockResolvedValue({ message_id: 'm2', thread_id: data.thread.id, direction: 'OUTBOUND', status: 'SENT' }),
    analyze: vi.fn().mockResolvedValue({ thread_id: data.thread.id, intent: 'INTERESTED', confidence: 0.95, unsubscribe: false, suppressed: false, requires_confirmation: false, warnings: [] }),
    assistantDraft: vi.fn().mockResolvedValue({ id: 'd2', thread_id: data.thread.id, subject: 'Re: Hello', body: 'AI generated reply', status: 'DRAFT', operation: 'DRAFT', intent: 'INTERESTED', intent_confidence: 0.95, summary: null, next_action: null, warnings: [], provider: 'MOCK', source_draft_id: null }),
    summarize: vi.fn().mockResolvedValue({ id: 's1', thread_id: data.thread.id, subject: null, body: null, status: 'DRAFT', operation: 'SUMMARIZE', intent: null, intent_confidence: null, summary: 'The prospect is interested.', next_action: null, warnings: [], provider: 'MOCK', source_draft_id: null }),
    nextAction: vi.fn().mockResolvedValue({ id: 's2', thread_id: data.thread.id, subject: null, body: null, status: 'DRAFT', operation: 'NEXT_ACTION', intent: 'INTERESTED', intent_confidence: 0.95, summary: null, next_action: 'Propose a meeting time.', warnings: [], provider: 'MOCK', source_draft_id: null }),
    transform: vi.fn().mockResolvedValue({ id: 'd3', thread_id: data.thread.id, subject: 'Re: Hello', body: 'Transformed reply', status: 'DRAFT', operation: 'PROFESSIONAL', intent: 'INTERESTED', intent_confidence: 0.95, summary: null, next_action: null, warnings: [], provider: 'MOCK', source_draft_id: 'd2' }),
    rejectDraft: vi.fn().mockResolvedValue({ id: 'd2', thread_id: data.thread.id, subject: 'Re: Hello', body: 'AI generated reply', status: 'REJECTED', operation: 'DRAFT', intent: 'INTERESTED', intent_confidence: 0.95, summary: null, next_action: null, warnings: [], provider: 'MOCK', source_draft_id: null }),
  },
}))

vi.mock('../api/senders', () => ({
  sendersApi: { list: vi.fn().mockResolvedValue([]) },
}))

function renderInbox() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><Inbox accessToken="token" /></QueryClientProvider>)
}

describe('Inbox', () => {
  it('shows the unified inbox heading and thread list', async () => {
    renderInbox()
    expect(screen.getByRole('heading', { name: /Inbox/i })).toBeInTheDocument()
    expect(screen.getByText('UNIFIED INBOX')).toBeInTheDocument()
    expect(await screen.findByText('Re: Hello')).toBeInTheDocument()
    expect(screen.getByText(/Interested, tell me more/)).toBeInTheDocument()
  })

  it('selecting a thread opens the conversation and recipient panes', async () => {
    const user = userEvent.setup()
    renderInbox()
    await user.click(await screen.findByText('Re: Hello'))
    await waitFor(() => expect(screen.getByText(/Interested!/)).toBeInTheDocument())
    expect(screen.getByText('Alice Smith')).toBeInTheDocument()
    expect(screen.getByText('Acme')).toBeInTheDocument()
    expect(screen.getByText('CTO')).toBeInTheDocument()
  })

  it('identifies intent via the AI assistant panel', async () => {
    const user = userEvent.setup()
    renderInbox()
    await user.click(await screen.findByText('Re: Hello'))
    await user.click(screen.getByRole('button', { name: /Identify intent/i }))
    await waitFor(() => expect(screen.getByText('Interested')).toBeInTheDocument())
    expect(screen.getByText(/95%/)).toBeInTheDocument()
  })

  it('generates a draft and supports approve/reject', async () => {
    const user = userEvent.setup()
    renderInbox()
    await user.click(await screen.findByText('Re: Hello'))
    await user.click(screen.getByRole('button', { name: /Generate reply draft/i }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Approve/i })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /Approve/i }))
    await waitFor(() => expect(screen.queryByRole('button', { name: /Approve/i })).not.toBeInTheDocument())
  })
})
