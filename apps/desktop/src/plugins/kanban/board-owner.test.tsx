import type { PluginRestOptions } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

// Host setup for a real board + drawer integration.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { bindApi, rememberTaskOwner, taskKey } from './api'
import { KanbanBoardPage } from './board'
import { KANBAN_LOCALES } from './i18n'
import { installOpenTaskHook, OPEN_TASK_HOOK } from './open-task'

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))

let dispose = () => {}

let locales = () => {}

let unhook = () => {}
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
afterEach(() => {
  cleanup()
  client.clear()
  dispose()
  locales()
  unhook()
  vi.unstubAllGlobals()
})

it('carries a qualified open request through the board, drawer, edits, uploads, download and related navigation', async () => {
  const owner = { gateway: 'owner-a', profile: 'research', board: 'ops' }
  const save = vi.fn().mockResolvedValue({ saved: true })
  vi.stubGlobal('hermesDesktop', { saveGatewayFile: save })

  const rest = vi.fn(async (path: string, opts?: PluginRestOptions) => {
    const url = new URL(path, 'http://example.com')

    if (url.pathname === '/boards') {
      return { current: 'default', boards: [] }
    }

    if (url.pathname === '/board') {
      return { columns: [], tenants: [], assignees: [], latest_event_id: 1 }
    }

    if (url.pathname === '/profiles') {
      return { profiles: [{ name: `${opts?.connectionId ?? 'ambient'}-worker` }] }
    }

    if (url.pathname === '/orchestration') {
      return { default_assignee: '' }
    }

    if (url.pathname.endsWith('/log')) {
      return { exists: false, content: '' }
    }

    if (opts?.method) {
      return { ok: true }
    }

    if (url.pathname.startsWith('/tasks/')) {
      const id = url.pathname.split('/')[2]

      return {
        task: { id, title: `${opts?.connectionId}/${opts?.profile}/${id}`, status: 'todo', body: 'Description' },
        comments: [],
        events: [],
        runs: [],
        links: { parents: id === 'same' ? ['related'] : [], children: [] },
        attachments: [{ id: 1, filename: 'report.md', stored_path: '/stored/report.md' }]
      }
    }

    throw new Error(`Unexpected ${path}`)
  })

  locales = registerPluginLocales('kanban', KANBAN_LOCALES)
  dispose = bindApi(rest as never, { get: (_key, fallback) => fallback, set: vi.fn(), remove: vi.fn() }, () => vi.fn())
  const target: Record<string, unknown> = {}
  unhook = installOpenTaskHook({ navigate: vi.fn(), rememberOwner: rememberTaskOwner }, target)
  const open = target[OPEN_TASK_HOOK] as (id: string, board: string, gateway: string, profile: string) => boolean
  render(
    <QueryClientProvider client={client}>
      <KanbanBoardPage />
    </QueryClientProvider>
  )
  act(() => {
    open('same', 'ops', 'owner-a', 'research')
  })
  // A same-id board refresh must not redirect the parked request or later actions.
  rememberTaskOwner('same', 'wrong', 'owner-b', 'default')
  await screen.findByRole('heading', { name: 'owner-a/research/same' })
  await waitFor(() => expect(rest).toHaveBeenCalledWith('/profiles', { connectionId: 'owner-a', profile: 'research' }))
  fireEvent.pointerDown(screen.getByRole('button', { name: 'unassigned' }), { button: 0, ctrlKey: false })
  fireEvent.click(await screen.findByRole('menuitem', { name: /owner-a-worker/ }))
  await waitFor(() =>
    expect(rest).toHaveBeenCalledWith(
      '/tasks/same/reassign?board=ops',
      expect.objectContaining({
        connectionId: 'owner-a',
        profile: 'research',
        body: { profile: 'owner-a-worker', reclaim_first: true }
      })
    )
  )
  fireEvent.change(screen.getByPlaceholderText('Add a comment…'), { target: { value: 'owner note' } })
  fireEvent.click(screen.getByRole('button', { name: 'Comment' }))
  await waitFor(() =>
    expect(rest).toHaveBeenCalledWith(
      '/tasks/same/comments?board=ops',
      expect.objectContaining({
        connectionId: 'owner-a',
        profile: 'research',
        body: { author: 'desktop', body: 'owner note' }
      })
    )
  )
  fireEvent.click(screen.getByRole('button', { name: 'Edit description' }))
  fireEvent.change(screen.getByDisplayValue('Description'), { target: { value: 'Updated' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() =>
    expect(rest).toHaveBeenCalledWith(
      '/tasks/same?board=ops',
      expect.objectContaining({
        connectionId: 'owner-a',
        profile: 'research',
        method: 'PATCH',
        body: { body: 'Updated' }
      })
    )
  )
  fireEvent.click(screen.getByRole('button', { name: 'Download report.md' }))
  await waitFor(() =>
    expect(save).toHaveBeenCalledWith({
      connectionId: 'owner-a',
      profile: 'research',
      path: '/stored/report.md',
      suggestedName: 'report.md'
    })
  )
  const file = new File(['a'], 'a.txt', { type: 'text/plain' })
  Object.defineProperty(file, 'arrayBuffer', { value: async () => new ArrayBuffer(1) })
  fireEvent.change(screen.getByRole('dialog').querySelector('input[type="file"]')!, { target: { files: [file] } })
  await waitFor(() =>
    expect(rest).toHaveBeenCalledWith(
      '/tasks/same/attachments?board=ops',
      expect.objectContaining({ connectionId: 'owner-a', profile: 'research', method: 'POST' })
    )
  )
  // Related ids are local to the same owning board, not globally unique.
  fireEvent.click(screen.getByRole('button', { name: 'relate' }))
  await screen.findByRole('heading', { name: 'owner-a/research/related' })
  // Start an upload on A, switch the same-id drawer to B while reading bytes,
  // then finish A. The old mutation must retain A's owner after unmount.
  let releaseBytes!: (bytes: ArrayBuffer) => void
  const delayed = new File(['b'], 'delayed.txt', { type: 'text/plain' })
  Object.defineProperty(delayed, 'arrayBuffer', {
    value: () =>
      new Promise<ArrayBuffer>(resolve => {
        releaseBytes = resolve
      })
  })
  fireEvent.change(screen.getByRole('dialog').querySelector('input[type="file"]')!, { target: { files: [delayed] } })
  await waitFor(() => expect(releaseBytes).toBeTypeOf('function'))
  act(() => {
    open('same', 'ops', 'owner-b', 'default')
  })
  await screen.findByRole('heading', { name: 'owner-b/default/same' })
  await act(async () => releaseBytes(new ArrayBuffer(1)))
  await waitFor(() =>
    expect(rest).toHaveBeenCalledWith(
      '/tasks/related/attachments?board=ops',
      expect.objectContaining({
        connectionId: 'owner-a',
        profile: 'research',
        upload: expect.objectContaining({ filename: 'delayed.txt' })
      })
    )
  )
  expect(client.getQueryData(taskKey('local', '', 'same', owner))).toMatchObject({
    task: { title: 'owner-a/research/same' }
  })

  for (const [path, opts] of rest.mock.calls.filter(([path]) => path.startsWith('/tasks/'))) {
    expect(opts?.connectionId).toMatch(/^owner-[ab]$/)
    expect(new URL(path, 'http://example.com').searchParams.get('board')).toBe('ops')
  }
})
