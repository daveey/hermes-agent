import '@/test/archive-queue-locks.setup'

import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type { HermesApiRequest } from '@/global'
import type { SessionInfo } from '@/hermes'
import { en } from '@/i18n/en'
import { startArchiveTrace } from '@/lib/archive-trace'
import { $confirmRequest, settleConfirm } from '@/store/confirm'
import { $notifications, dismissNotification } from '@/store/notifications'

import { commitArchive } from './archive-commit'
import { archivePreflight } from './archive-soft-hide'
import { confirmLinkedArchive } from './confirm-linked-archive'
import { reportDroppedArchiveQueues } from './reconnect-archive-preflight'

const row = { id: 'durable-archive', connection_id: 'hub', profile: 'work', kanban_unknown: true } as SessionInfo
const hidden = () => ({ end: vi.fn(), hid: true, ids: [row.id], restore: vi.fn() })

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.clear()
  sessionStorage.clear()
  $notifications.set([])
})
afterEach(() => {
  settleConfirm(false)
  $notifications.set([])
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

it('holds one attempt past five minutes, then reruns full safety and commits without another click', async () => {
  const started = Date.now()
  const logLine = vi.fn()
  const paths: string[] = []
  let archived = false

  const api = vi.fn(async ({ path, method, connectionId, profile }: HermesApiRequest) => {
    expect([connectionId, profile]).toEqual(['hub', 'work'])
    paths.push(path)

    if (Date.now() - started < 360_000) {
      throw new Error('Timed out connecting to Hermes backend after 6000ms')
    }

    if (path.includes('mutation-snapshot')) {
      return { exists: true, runtime_revision: 0, runtime_generation: 1 }
    }

    if (method === 'PATCH') {
      archived = true

      return { ok: true }
    }

    if (path === `/api/sessions/${row.id}`) {
      return { id: row.id, parent_session_id: 'root', archived }
    }

    if (path === '/api/sessions/root') {
      return { id: 'root' }
    }

    return { tasks: [] }
  })

  vi.stubGlobal('hermesDesktop', { api, logLine })
  const trace = startArchiveTrace(row.id)
  let outcome: boolean | undefined

  const pending = (async () => {
    if (!(await confirmLinkedArchive(row.id, row, 'work', en.desktop, false, trace))) {
      return false
    }

    return commitArchive({
      storedSessionId: row.id,
      owner: { connectionId: 'hub', profile: 'work' },
      hidden: hidden(),
      rehide: hidden,
      copy: en.desktop,
      onArchived: vi.fn(),
      retryLabel: 'Retry',
      trace
    })
  })().then(
    value => {
      outcome = value
    },
    () => {
      outcome = false
    }
  )

  await vi.advanceTimersByTimeAsync(350_000)

  const whileDisconnected = {
    outcome,
    archived,
    queued: $notifications.get().some(n => n.message === en.desktop.archiveQueued),
    markers: Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index) ?? '').filter(k =>
      k.startsWith('hermes:archive-preflight-queue:')
    ).length
  }

  await vi.advanceTimersByTimeAsync(30_000)
  await pending
  expect(whileDisconnected).toEqual({ outcome: undefined, archived: false, queued: true, markers: 1 })
  expect(outcome).toBe(true)
  expect(paths).toContain('/api/sessions/root')
  expect(paths).toContain('/api/session-kanban-tasks?ids=durable-archive%2Croot')
  const stages = logLine.mock.calls.flat().filter((s: string) => s.startsWith('[archive-trace]'))
  expect(stages.every((s: string) => s.startsWith(`[archive-trace] ${row.id} ${trace.attemptId} `))).toBe(true)
  expect(stages.filter((s: string) => s.endsWith(' start'))).toHaveLength(1)
  expect(stages.at(-1)).toContain(' committed')
  expect(
    Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index) ?? '').filter(k =>
      k.startsWith('hermes:archive-preflight-queue:')
    )
  ).toHaveLength(0)
})

it.each(['button', 'dismiss'])(
  'cancels through %s, restores immediately and ignores a late successful read',
  async mode => {
    let ready = false
    let answer!: (row: unknown) => void

    const api = vi.fn(async (_request: HermesApiRequest) => {
      if (!ready) {
        throw new Error('socket hang up')
      }

      return new Promise(resolve => {
        answer = resolve
      })
    })

    vi.stubGlobal('hermesDesktop', { api })
    const h = hidden()
    const pending = archivePreflight(row.id, row, true, h, en.desktop)
    await vi.advanceTimersByTimeAsync(0)
    ready = true
    await vi.advanceTimersByTimeAsync(1_000)
    const queued = $notifications.get().find(n => n.message === en.desktop.archiveQueued)!

    if (mode === 'button') {
      queued.action?.onClick()
    } else {
      dismissNotification(queued.id)
    }

    await vi.advanceTimersByTimeAsync(0)
    expect(h.restore).toHaveBeenCalledOnce()
    expect(await pending).toBe(false)
    expect($notifications.get().some(n => n.kind === 'error')).toBe(true)
    answer({ id: row.id })
    await vi.advanceTimersByTimeAsync(400_000)
    expect(api).toHaveBeenCalledTimes(2)
    expect($confirmRequest.get()).toBeNull()
    expect(
      Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index) ?? '').filter(k =>
        k.startsWith('hermes:archive-preflight-queue:')
      )
    ).toHaveLength(0)
  }
)

it('reports orphaned restart markers across window types without sessionStorage', async () => {
  localStorage.setItem('hermes:archive-preflight-queue:main:old-attempt', row.id)
  localStorage.setItem('hermes:archive-preflight-queue:other-window:active-attempt', 'other')
  await reportDroppedArchiveQueues()
  expect($notifications.get().find(n => n.id === 'archive-queue-reload')?.message).toBe(en.desktop.archiveQueueDropped)
  expect(localStorage.getItem('hermes:archive-preflight-queue:main:old-attempt')).toBeNull()
  expect(localStorage.getItem('hermes:archive-preflight-queue:other-window:active-attempt')).toBeNull()
})

it('keeps non-transport unknown as a warning confirmation, never automatic permission', async () => {
  vi.stubGlobal('hermesDesktop', { api: vi.fn(async () => ({ malformed: true })) })
  const pending = confirmLinkedArchive(row.id, row, 'work', en.desktop)
  await vi.advanceTimersByTimeAsync(0)
  expect($confirmRequest.get()?.details?.length).toBeGreaterThan(0)
  expect($notifications.get().some(n => n.message === en.desktop.archiveQueued)).toBe(false)
  settleConfirm(false)
  expect(await pending).toBe(false)
})
