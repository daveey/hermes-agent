import type { PluginRestOptions } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// eslint-disable-next-line no-restricted-imports
import { onGatewayEvent } from '@/contrib/events'
// Host setup for a real board + drawer integration.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'
// eslint-disable-next-line no-restricted-imports
import { peekUiLatency, resetUiLatencyForTests } from '@/lib/ui-latency'

import { bindApi } from './api'
import { KanbanBoardPage } from './board'
import { KANBAN_LOCALES } from './i18n'

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))

let dispose = () => {}

let locales = () => {}

let off = () => {}
let events: Array<Record<string, unknown>> = []
let failDetail = false
let failCreate = false
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const rest = vi.fn(async (path: string, opts?: PluginRestOptions) => {
  const url = new URL(path, 'http://example.com')

  if (url.pathname === '/boards') {
    return { current: 'default', boards: [] }
  }

  if (url.pathname === '/board') {
    return {
      assignees: [],
      columns: [{ name: 'ready', tasks: [{ id: 't_card', status: 'ready', title: 'Card one' }] }],
      latest_event_id: 1,
      tenants: []
    }
  }

  if (url.pathname === '/profiles') {
    return { profiles: [] }
  }

  if (url.pathname === '/orchestration') {
    return { default_assignee: '' }
  }

  if (url.pathname.endsWith('/log')) {
    return { content: '', exists: false }
  }

  if (url.pathname === '/tasks' && opts?.method === 'POST') {
    if (failCreate) {
      throw new Error('create refused')
    }

    return { task: { id: 't_new', status: 'ready', title: 'Brand new' } }
  }

  if (url.pathname === '/tasks/t_card') {
    if (failDetail) {
      throw new Error('detail refused')
    }

    return {
      attachments: [],
      comments: [],
      events: [],
      links: { children: [], parents: [] },
      runs: [],
      task: { body: 'Description', id: 't_card', status: 'ready', title: 'Card one' }
    }
  }

  if (opts?.method) {
    return { ok: true }
  }

  throw new Error(`Unexpected ${path}`)
})

beforeEach(() => {
  events = []
  failDetail = false
  failCreate = false
  rest.mockClear()
  locales = registerPluginLocales('kanban', KANBAN_LOCALES)
  dispose = bindApi(rest as never, { get: (_key, fallback) => fallback, remove: vi.fn(), set: vi.fn() }, () => vi.fn())
  off = onGatewayEvent('desktop.ui_latency', event => events.push({ ...(event.payload as Record<string, unknown>) }))
})

afterEach(() => {
  cleanup()
  client.clear()
  dispose()
  locales()
  off()
  resetUiLatencyForTests()
})

const mount = () =>
  render(
    <QueryClientProvider client={client}>
      <KanbanBoardPage />
    </QueryClientProvider>
  )

describe('ui.card_read through the real board and drawer', () => {
  it('starts at the card click and ends only after the drawer committed THAT card (and two frames)', async () => {
    mount()
    fireEvent.click(await screen.findByText('Card one'))

    // The click itself started the span; the request has not painted anything yet.
    expect(peekUiLatency('ui.card_read')?.ids).toMatchObject({ card_id: 't_card' })
    expect(events).toEqual([])

    await screen.findByRole('heading', { name: 'Card one' })
    await waitFor(() => expect(events).toHaveLength(1))
    expect(events[0]).toMatchObject({ card_id: 't_card', name: 'ui.card_read' })
    // Identifiers only: nothing outside the contract (no title, body or path) is ever attached.
    const allowed = ['board', 'card_id', 'duration_ms', 'gateway', 'name', 'profile', 'session_id', 'start_ms']
    expect(Object.keys(events[0]).every(key => allowed.includes(key))).toBe(true)
    expect(JSON.stringify(events[0])).not.toContain('Card one')
  })

  it('a failed detail read never reports success', async () => {
    failDetail = true
    mount()
    fireEvent.click(await screen.findByText('Card one'))

    await waitFor(() => expect(peekUiLatency('ui.card_read')).toBeNull())
    await act(async () => void (await new Promise(resolve => setTimeout(resolve, 80))))
    expect(events).toEqual([])
  })

  it('closing the drawer before it paints does not report', async () => {
    mount()
    fireEvent.click(await screen.findByText('Card one'))
    fireEvent.click(await screen.findByRole('button', { name: 'Close' }))
    await act(async () => void (await new Promise(resolve => setTimeout(resolve, 80))))

    expect(events).toEqual([])
  })
})

describe('ui.card_create through the real new-task dialog', () => {
  const openDialog = async () => {
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /New task in/ }))
    fireEvent.change(await screen.findByPlaceholderText('Title'), { target: { value: 'Brand new' } })
  }

  it('ends when the create call succeeded, carrying the new card id', async () => {
    await openDialog()
    fireEvent.click(screen.getByRole('button', { name: 'Create task' }))

    await waitFor(() => expect(events).toHaveLength(1))
    expect(events[0]).toMatchObject({ card_id: 't_new', name: 'ui.card_create' })
    expect(events[0].duration_ms).toBeGreaterThanOrEqual(0)
  })

  it('a refused create reports nothing and leaves no span', async () => {
    failCreate = true
    await openDialog()
    fireEvent.click(screen.getByRole('button', { name: 'Create task' }))

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith(expect.stringContaining('/tasks'), expect.objectContaining({ method: 'POST' }))
    )
    await waitFor(() => expect(peekUiLatency('ui.card_create')).toBeNull())
    expect(events).toEqual([])
  })
})
