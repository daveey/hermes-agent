import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type { HermesApiRequest } from '@/global'
import type { SessionInfo } from '@/hermes'
import { en } from '@/i18n/en'
import { $confirmRequest, settleConfirm } from '@/store/confirm'
import { $notifications, clearNotifications } from '@/store/notifications'

import { archivePreflight } from './archive-soft-hide'
import { refreshArchiveTasks } from './refresh-archive-tasks'

const row = { id: 'deadline', connection_id: 'owner', profile: 'work', kanban_unknown: true } as SessionInfo

beforeEach(() => {
  vi.useFakeTimers()
  sessionStorage.clear()
  clearNotifications()
})
afterEach(() => {
  settleConfirm(false)
  clearNotifications()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

it('deducts successful ancestry time before the task request timeout', async () => {
  const api = vi.fn(({ path, timeoutMs }: HermesApiRequest) => new Promise((resolve, reject) => {
    if (path === '/api/sessions/deadline') {
      setTimeout(() => resolve({ id: row.id }), 1_000)
    } else {
      setTimeout(() => reject(new Error('Timed out connecting to Hermes backend')), (timeoutMs ?? 0) + 1)
    }
  }))

  vi.stubGlobal('hermesDesktop', { api })

  const outcome = refreshArchiveTasks(row.id, row, 'work', { retryTransport: true, budgetMs: 5_000 })
    .then(() => 'resolved', () => 'rejected')

  await vi.advanceTimersByTimeAsync(6_000)
  expect(await outcome).toBe('rejected')
  expect(api.mock.calls[1][0].timeoutMs).toBe(4_000)
})

it('rejects the reconnect deadline even if a transport read never settles', async () => {
  const api = vi.fn(() => new Promise(() => {}))
  vi.stubGlobal('hermesDesktop', { api })

  const outcome = refreshArchiveTasks(row.id, row, 'work', { retryTransport: true, budgetMs: 125 })
    .then(() => 'resolved', () => 'rejected')

  await vi.advanceTimersByTimeAsync(375)
  expect(await outcome).toBe('rejected')
})

it('does not accept a task result arriving after the shared deadline', async () => {
  const api = vi.fn(({ path }: HermesApiRequest) => {
    if (path === '/api/sessions/deadline') {return Promise.resolve({ id: row.id })}

    return new Promise(resolve => setTimeout(() => resolve({ tasks: [] }), 5_001))
  })

  vi.stubGlobal('hermesDesktop', { api })

  const outcome = refreshArchiveTasks(row.id, row, 'work', { retryTransport: true, budgetMs: 5_000 })
    .then(() => 'resolved', () => 'rejected')

  await vi.advanceTimersByTimeAsync(5_250)
  expect(await outcome).toBe('rejected')
})

it('keeps sequential transport read deadlines queued rather than offering Archive anyway', async () => {
  let attempts = 0

  const api = vi.fn(({ path, timeoutMs }: HermesApiRequest) => {
    if (path === '/api/sessions/deadline') {
      if (++attempts < 9) {return Promise.reject(new Error('socket hang up'))}

      return new Promise(resolve => setTimeout(() => resolve({ id: row.id }), 1_000))
    }

    return new Promise((_resolve, reject) => setTimeout(() => reject(new Error('Timed out connecting to Hermes backend')), (timeoutMs ?? 0) + 1))
  })

  vi.stubGlobal('hermesDesktop', { api })
  const hidden = { end: vi.fn(), hid: true, ids: [row.id], restore: vi.fn() }
  const outcome = archivePreflight(row.id, row, true, hidden, en.desktop)
  await vi.advanceTimersByTimeAsync(61_000)
  const confirmation = $confirmRequest.get()
  expect(hidden.restore).not.toHaveBeenCalled()
  $notifications.get().find(n => n.message === en.desktop.archiveQueued)?.action?.onClick()
  expect(await outcome).toBe(false)
  expect(confirmation).toBeNull()
  expect(hidden.restore).toHaveBeenCalledOnce()
  expect(api.mock.calls.some(([request]) => request.method === 'PATCH')).toBe(false)
})
