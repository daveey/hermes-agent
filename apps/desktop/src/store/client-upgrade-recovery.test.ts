import { afterEach, beforeEach, expect, test, vi } from 'vitest'

const { openUpdatesWindow, cloud } = vi.hoisted(() => ({ openUpdatesWindow: vi.fn(), cloud: { served: false } }))
vi.mock('@/store/updates', () => ({ openUpdatesWindow }))
vi.mock('@/lib/hermes-cloud-host', () => ({ servedByHermesCloud: () => cloud.served }))

const refusal = new Error('409: {"detail":"client_upgrade_required: this app is out of sync (different versions) with the Hermes backend; update the app to edit sessions"}')

beforeEach(() => {
  localStorage.clear()
  vi.resetModules()
  openUpdatesWindow.mockClear()
  cloud.served = false
})
afterEach(() => vi.restoreAllMocks())

test('an actual client upgrade refusal survives unrelated notification clearing and renderer reload', async () => {
  const first = await import('./notifications')
  first.notifyError(refusal, 'Edit failed')
  first.clearNotifications()
  expect(first.$notifications.get()).toHaveLength(1)
  expect(first.$notifications.get()[0]).toMatchObject({ persistent: true, kind: 'error' })
  vi.resetModules()
  const reloaded = await import('./notifications')
  expect(reloaded.$notifications.get()).toHaveLength(1)
  expect(reloaded.$notifications.get()[0].message).toBe(first.$notifications.get()[0].message)
  expect(JSON.stringify(localStorage)).not.toContain('Edit failed')
})

test('client recovery opens the client updater, never the active remote backend, without applying it', async () => {
  const n = await import('./notifications')
  n.notifyError(refusal, 'Edit failed')
  n.$notifications.get()[0].action?.onClick()
  await vi.waitFor(() => expect(openUpdatesWindow).toHaveBeenCalledWith('client'))
})

test('explicit dismissal acknowledges the durable note, but another refusal restores it', async () => {
  const n = await import('./notifications')
  const id = n.notifyError(refusal, 'Edit failed')
  n.dismissNotification(id)
  vi.resetModules()
  const reloaded = await import('./notifications')
  expect(reloaded.$notifications.get()).toEqual([])
  reloaded.notifyError(refusal, 'Edit failed again')
  expect(reloaded.$notifications.get()).toHaveLength(1)
})

test('repeat refusals coalesce even when callers supply different toast IDs', async () => {
  const n = await import('./notifications')
  n.notifyError(refusal, 'Edit failed', { id: 'first' })
  n.notifyError(refusal, 'Edit failed', { id: 'second' })
  expect(n.$notifications.get()).toHaveLength(1)
})

test('reload alone restores the note and opening Updates does not acknowledge compatibility', async () => {
  const n = await import('./notifications')
  n.notifyError(refusal, 'Edit failed')
  n.$notifications.get()[0].action?.onClick()
  await vi.waitFor(() => expect(openUpdatesWindow).toHaveBeenCalledWith('client'))
  vi.resetModules()
  const reloaded = await import('./notifications')
  expect(reloaded.$notifications.get()).toHaveLength(1)
  expect(reloaded.$notifications.get()[0]).toMatchObject({ persistent: true, kind: 'error' })
})

test('Cloud offers reload instead of a native update', async () => {
  cloud.served = true
  const n = await import('./notifications')
  n.notifyError(refusal, 'Edit failed')
  expect(n.$notifications.get()[0].action?.label).not.toBe('Update Hermes')
  expect(n.$notifications.get()[0].action?.label).toMatch(/reload/i)
  expect(openUpdatesWindow).not.toHaveBeenCalled()
})

test('unavailable localStorage does not hide the in-window recovery', async () => {
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied') })
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
  const n = await import('./notifications')
  n.notifyError(refusal, 'Edit failed')
  n.clearNotifications()
  expect(n.$notifications.get()).toHaveLength(1)
})

test('bare legacy refusal codes also retain recovery, without depending on the wording suffix', async () => {
  const n = await import('./notifications')
  n.notifyError(new Error('409: client_upgrade_required'), 'Edit failed')
  n.clearNotifications()
  expect(n.$notifications.get()).toHaveLength(1)
  expect(n.$notifications.get()[0].message).toMatch(/different versions/i)
})

test('ordinary errors and generic protocol skew keep their existing transient policy', async () => {
  const n = await import('./notifications')
  n.notifyError(new Error('out of sync (different versions)'), 'RPC failed')
  n.notifyError(new Error('network timeout'), 'Edit failed')
  n.clearNotifications()
  expect(n.$notifications.get()).toEqual([])
  vi.resetModules()
  expect((await import('./notifications')).$notifications.get()).toEqual([])
})
