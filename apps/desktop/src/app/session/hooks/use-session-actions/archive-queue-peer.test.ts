import { afterEach, expect, it, vi } from 'vitest'

import { en } from '@/i18n/en'
import { archiveQueueLocksFixture } from '@/test/web-locks'

const markers = () => Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i)!).filter(k => k.startsWith('hermes:archive-preflight-queue:'))

afterEach(() => {
  vi.useRealTimers()
  localStorage.clear()
  sessionStorage.clear()
  vi.unstubAllGlobals()
})

it.each(['?peer=1', '?win=secondary'])('does not cancel another live renderer with the same window type: %s', async search => {
  const fixture = archiveQueueLocksFixture()
  vi.stubGlobal('navigator', { locks: fixture.locks })
  vi.stubGlobal('window', new Proxy(window, { get(target, prop) {
    return prop === 'location' ? { search } : Reflect.get(target, prop)
  } }))
  vi.resetModules()
  const owner = await import('./archive-queue-lifetime')
  const queue = owner.archiveQueueLifetime('live-attempt', 'live-chat', en.desktop)
  await queue.enqueue()
  const key = markers()[0]
  expect(key).toBeTruthy()
  vi.resetModules()
  const peer = await import('./archive-queue-lifetime')
  const { $notifications } = await import('@/store/notifications')
  await peer.reportDroppedArchiveQueues()
  expect(localStorage.getItem(key)).toBe('live-chat')
  expect($notifications.get().find(n => n.id === 'archive-queue-reload')).toBeUndefined()
  expect(queue.signal.aborted).toBe(false)
  queue.dispose()
  await vi.waitFor(() => expect(fixture.held.size).toBe(0))
  $notifications.set([])
})

it('a peer mount during a six-minute outage preserves the queue until fresh preflight recovers', async () => {
  vi.useFakeTimers()
  const fixture = archiveQueueLocksFixture()
  vi.stubGlobal('navigator', { locks: fixture.locks })
  let online = false

  const api = vi.fn(async ({ path }: { path: string }) => {
    if (!online) {throw new Error('socket hang up')}

    return path.includes('session-kanban-tasks') ? { tasks: [] } : { id: 'peer-chat' }
  })

  vi.stubGlobal('hermesDesktop', { api })
  vi.resetModules()
  const { confirmLinkedArchive } = await import('./confirm-linked-archive')
  const pending = confirmLinkedArchive('peer-chat', { id: 'peer-chat', connection_id: 'hub', profile: 'work', kanban_unknown: true } as never, 'work', en.desktop)
  await vi.advanceTimersByTimeAsync(360_000)
  const key = markers()[0]
  expect(localStorage.getItem(key)).toBe('peer-chat')
  vi.resetModules()
  const peer = await import('./archive-queue-lifetime')
  const { $notifications } = await import('@/store/notifications')
  await peer.reportDroppedArchiveQueues()
  expect(localStorage.getItem(key)).toBe('peer-chat')
  expect($notifications.get().find(n => n.id === 'archive-queue-reload')).toBeUndefined()
  online = true
  await vi.advanceTimersByTimeAsync(10_000)
  expect(await pending).toBe(true)
  expect(localStorage.getItem(key)).toBeNull()
  $notifications.set([])
})

it('a new app renderer reports an orphan after its old renderer releases custody', async () => {
  const fixture = archiveQueueLocksFixture()
  vi.stubGlobal('navigator', { locks: fixture.locks })
  const key = 'hermes:archive-preflight-queue:vanished-renderer:attempt'
  localStorage.setItem(key, 'orphan-chat')
  sessionStorage.clear()
  vi.resetModules()
  const fresh = await import('./archive-queue-lifetime')
  const { $notifications } = await import('@/store/notifications')
  await fresh.reportDroppedArchiveQueues()
  expect(localStorage.getItem(key)).toBeNull()
  expect($notifications.get().find(n => n.id === 'archive-queue-reload')?.message).toBe(en.desktop.archiveQueueDropped)
  $notifications.set([])
})

it('fails visibly rather than queueing without origin-wide custody support', async () => {
  vi.stubGlobal('navigator', {})
  const { archiveQueueLifetime } = await import('./archive-queue-lifetime')
  const queue = archiveQueueLifetime('unsupported', 'chat', en.desktop)
  await expect(queue.enqueue()).rejects.toThrow('requires Web Locks')
  expect(markers()).toHaveLength(0)
  queue.dispose()
})

it('does not persist an intent disposed while its lock acquisition was pending', async () => {
  const fixture = archiveQueueLocksFixture()
  let grant!: () => void
  const waiting = new Promise<void>(resolve => {grant = resolve})
  vi.stubGlobal('navigator', { locks: { request: async (...args: Parameters<typeof fixture.locks.request>) => {
    await waiting

    return fixture.locks.request(...args)
  } } })
  const { archiveQueueLifetime } = await import('./archive-queue-lifetime')
  const queue = archiveQueueLifetime('disposed', 'chat', en.desktop)
  const enqueue = queue.enqueue()
  queue.dispose()
  grant()
  await expect(enqueue).rejects.toThrow(en.desktop.archiveQueueCancelled)
  expect(markers()).toHaveLength(0)
  await vi.waitFor(() => expect(fixture.held.size).toBe(0))
})
