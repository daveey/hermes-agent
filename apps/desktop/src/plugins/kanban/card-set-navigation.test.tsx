import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ComponentType } from 'react'
import { afterEach, expect, it, vi } from 'vitest'

// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import type * as API from './api'
import { $cardSet, CardSetBoard, installCardSetHook, SHOW_CARDS_HOOK } from './card-set'
import { KANBAN_LOCALES } from './i18n'
import { $openTaskRequest } from './open-task'
import plugin from './plugin'
import { $newTaskLane } from './ui'

vi.mock('./api', async original => ({ ...(await original<typeof API>()), bindApi: () => () => {} }))
vi.mock('./board', () => ({ KanbanBoardPage: () => <div>ordinary board</div> }))
vi.mock('./board-task-drawer', () => ({
  BoardTaskDrawer: ({
    task,
    columns
  }: {
    task: { board: string; taskId: string; owner?: { gateway: string; board: string; profile: string } }
    columns: string[]
  }) => (
    <div
      data-board={task.board}
      data-columns={JSON.stringify(columns)}
      data-owner={JSON.stringify(task.owner)}
      data-testid="drawer"
    >
      {task.owner?.gateway}/{task.taskId}
    </div>
  )
}))

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const disposers: (() => void)[] = []
afterEach(() => {
  cleanup()
  disposers.splice(0).forEach(dispose => dispose())
  client.clear()
  $cardSet.set(null)
  $openTaskRequest.set(null)
  $newTaskLane.set(null)
})

function setup() {
  disposers.push(registerPluginLocales('kanban', KANBAN_LOCALES))
  const rest = vi.fn(async () => ({
    columns: [{ name: 'ready', tasks: [{ id: 'one', title: 'selected card', status: 'ready' }] }]
  }))

  return rest
}

it.each(['open', 'new'] as const)('ordinary %s intent leaves the mounted card-set route', async intent => {
  const rest = setup()
  let Page: ComponentType | undefined
  plugin.register({
    rest,
    i18n: { register() {}, t: (key: string) => key, onLocaleChange() {} },
    onDispose: (dispose: () => void) => disposers.push(dispose),
    storage: { get: (_key: string, fallback: unknown) => fallback, set() {} },
    registerMany: (entries: { id: string; render?: ComponentType }[]) => {
      Page ??= entries.find(entry => entry.id === 'page')?.render

      return () => {}
    }
  } as unknown as Parameters<typeof plugin.register>[0])
  act(() => $cardSet.set({ connectionId: 'owner-a', cards: [{ board: 'alpha', id: 'one' }] }))
  expect(Page).toBeDefined()
  render(<QueryClientProvider client={client}>{Page && <Page />}</QueryClientProvider>)
  await screen.findByText('selected card')
  act(() => {
    if (intent === 'open') {
      $openTaskRequest.set({ board: 'beta', taskId: 'two' })
    } else {
      $newTaskLane.set('triage')
    }
  })
  expect(screen.getByText('ordinary board')).toBeTruthy()
  expect($cardSet.get()).toBeNull()
})

it('routes the drawer to its exact owner and only that board’s status columns', async () => {
  setup()

  const rest = vi.fn(async (path: string) => {
    const board = new URL(path, 'https://example.com').searchParams.get('board')

    return {
      columns: [
        { name: 'ready', tasks: [{ id: 'one', title: `${board}/selected`, status: 'ready' }] },
        { name: board === 'alpha' ? 'blocked' : 'review', tasks: [] }
      ]
    }
  })

  act(() =>
    $cardSet.set({
      connectionId: 'owner-a',
      cards: [
        { board: 'alpha', id: 'one' },
        { board: 'beta', id: 'one' }
      ]
    })
  )
  render(
    <QueryClientProvider client={client}>
      <CardSetBoard rest={rest as never} />
    </QueryClientProvider>
  )
  fireEvent.click(await screen.findByText('beta/selected'))
  const drawer = screen.getByTestId('drawer')
  expect(drawer.getAttribute('data-board')).toBe('beta')
  expect(JSON.parse(drawer.getAttribute('data-owner')!)).toEqual({
    board: 'beta',
    gateway: 'owner-a',
    profile: 'default'
  })
  expect(JSON.parse(drawer.getAttribute('data-columns')!)).toEqual(['ready', 'review'])
})

it('replacing the selection closes the prior owner drawer before the next card is opened', async () => {
  const rest = setup()
  const target: Record<string, unknown> = {}
  disposers.push(installCardSetHook({ navigate: vi.fn() }, target))
  const show = target[SHOW_CARDS_HOOK] as (request: unknown) => boolean
  act(() => {
    show({ connectionId: 'owner-a', cards: [{ board: 'alpha', id: 'one' }] })
  })
  render(
    <QueryClientProvider client={client}>
      <CardSetBoard rest={rest as never} />
    </QueryClientProvider>
  )
  fireEvent.click(await screen.findByText('selected card'))
  expect(screen.getByTestId('drawer').textContent).toBe('owner-a/one')
  act(() => {
    show({ connectionId: 'owner-b', cards: [{ board: 'beta', id: 'one' }] })
  })
  expect(screen.queryByTestId('drawer')).toBeNull()
  fireEvent.click(await screen.findByText('selected card'))
  expect(screen.getByTestId('drawer').textContent).toBe('owner-b/one')
})
