import { afterEach, expect, it, vi } from 'vitest'

import { en } from '@/i18n/en'
import { $notifications } from '@/store/notifications'
import { archiveQueueLocksFixture } from '@/test/web-locks'

import { archiveQueueLifetime } from './archive-queue-lifetime'

const queueLocks = archiveQueueLocksFixture()
Object.defineProperty(navigator, 'locks', { configurable: true, value: queueLocks.locks })

afterEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  $notifications.set([])
  vi.useRealTimers()
})

it('shows a restart drop note from the marker written by a real queue, with a new renderer module and no sessionStorage', async () => {
  const queue = archiveQueueLifetime('renderer-before-restart', 'restart-chat', en.desktop)
  await queue.enqueue()
  expect(Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index) ?? '')).toContain('hermes:archive-preflight-queue:renderer-before-restart')
  sessionStorage.clear()
  queueLocks.held.clear() // Renderer destruction releases Chromium's locks.
  // A reload/app restart loses module memory, but not the durable marker.
  vi.resetModules()
  const { reportDroppedArchiveQueues } = await import('./archive-queue-lifetime')
  const { $notifications: freshNotifications } = await import('@/store/notifications')
  await reportDroppedArchiveQueues()
  expect(freshNotifications.get().find(n => n.id === 'archive-queue-reload')).toMatchObject({
    kind: 'warning', persistent: true, message: en.desktop.archiveQueueDropped
  })
  expect(localStorage.getItem('hermes:archive-preflight-queue:renderer-before-restart')).toBeNull()
  await reportDroppedArchiveQueues()
  expect(freshNotifications.get().filter(n => n.id === 'archive-queue-reload')).toHaveLength(1)
  queue.dispose()
  freshNotifications.set([])
})

it('cancels a queued backoff immediately, without leaving its timer or marker behind', async () => {
  vi.useFakeTimers()
  const queue = archiveQueueLifetime('backoff-cancel', 'cancel-chat', en.desktop)
  await queue.enqueue()
  const baselineTimers = vi.getTimerCount()
  const waiting = queue.sleep(10_000).then(() => 'slept', error => error.message)
  $notifications.get().find(n => n.message === en.desktop.archiveQueued)?.action?.onClick()
  expect(await waiting).toBe(en.desktop.archiveQueueCancelled)
  expect(vi.getTimerCount()).toBeLessThanOrEqual(baselineTimers)
  queue.dispose()
  expect(Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index) ?? '')).toHaveLength(0)
})
