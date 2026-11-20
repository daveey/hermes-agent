/**
 * A shadow board's card another gateway owns is merged in read-only by the backend (`remote`, `home_gateway`):
 * the board marks it with its home gateway and leaves ordinary cards unmarked.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import type * as KanbanApi from './api'
import { $boardSlug } from './api'
import { KanbanBoardPage } from './board'
import { KANBAN_LOCALES } from './i18n'

vi.mock('./api', async importOriginal => ({
  ...(await importOriginal<typeof KanbanApi>()),
  fetchBoards: vi.fn(async () => ({ boards: [{ name: 'BB', project_id: null, slug: 'bb', total: 2 }], current: 'bb' })),
  fetchBoard: vi.fn(async () => ({
    assignees: [],
    columns: [
      {
        name: 'ready',
        tasks: [
          { id: 't_1a2b3c', status: 'ready', title: 'Hub card' },
          { home_gateway: 'mini', id: 't_4d5e6f', read_only: true, remote: true, status: 'ready', title: 'BB-12 feature' }
        ]
      }
    ],
    latest_event_id: 0,
    now: 0,
    tenants: []
  })),
  fetchOrchestration: vi.fn(async () => ({ default_assignee: '' })),
  fetchProfiles: vi.fn(async () => ({ profiles: [] }))
}))

afterEach(() => {
  cleanup()
  $boardSlug.set('')
})

const tileOf = (title: string) => screen.getByText(title).closest<HTMLElement>('[draggable="true"]')!

describe('kanban board: remote shadow-replica cards', () => {
  it('marks a remote card with its home gateway and leaves local cards unmarked', async () => {
    const dispose = registerPluginLocales('kanban', KANBAN_LOCALES)

    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <KanbanBoardPage />
      </QueryClientProvider>
    )
    await screen.findByText('BB-12 feature')

    expect(within(tileOf('BB-12 feature')).getByText('remote · mini')).toBeTruthy()
    expect(tileOf('Hub card').querySelector('[data-slot="kanban-card-remote"]')).toBeNull()
    dispose()
  })
})
