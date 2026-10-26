import { afterEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/hermes'

import { activeConnection, pluginRest } from './plugins'

// Owner pins must survive an ambient profile/gateway change.
describe('plugin REST qualified owner', () => {
  afterEach(() => {
    setApiRequestConnection(null)
    setApiRequestProfile(null)
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it.each([
    { opts: {}, expected: { connectionId: 'ambient-gw', profile: 'ambient' } },
    { opts: { connectionId: 'owner' }, expected: { connectionId: 'owner' } },
    { opts: { connectionId: 'owner', profile: null }, expected: { connectionId: 'owner' } },
    { opts: { profile: null }, expected: { connectionId: 'ambient-gw' } },
    { opts: { profile: 'worker' }, expected: { connectionId: 'ambient-gw', priority: 'foreground', profile: 'worker' } }
  ])('preserves scoped semantics for $opts', async ({ opts, expected }) => {
    const api = vi.fn().mockResolvedValue({ ok: true })
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api } })
    setApiRequestConnection('ambient-gw')
    setApiRequestProfile('ambient')
    await pluginRest('kanban', '/tasks/t_one', opts)
    expect(api.mock.calls[0][0]).toEqual({
      body: undefined, method: undefined, path: '/api/plugins/kanban/tasks/t_one',
      timeoutMs: undefined, upload: undefined, ...expected
    })
  })

  it('carries the explicit profile to its owner gateway, including multipart writes', async () => {
    const api = vi.fn().mockResolvedValue({ ok: true })
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api } })
    setApiRequestConnection('other')
    setApiRequestProfile('ambient')
    const upload = { bytes: new ArrayBuffer(0), filename: 'proof.txt' }
    await pluginRest('kanban', '/tasks/t_one/attachments?board=ops', {
      connectionId: 'owner', method: 'POST', profile: 'worker', upload
    })
    expect(api).toHaveBeenCalledWith({
      body: undefined, connectionId: 'owner', method: 'POST',
      path: '/api/plugins/kanban/tasks/t_one/attachments?board=ops',
      priority: 'foreground', profile: 'worker', timeoutMs: undefined, upload
    })
  })
})

// desktop.getConnection/getConnectionFor are IPC round-trips into the main
// process with no timeout of their own (#93454). A wedged main-process
// round-trip must reject instead of hanging pluginSocket's connect() forever.
describe('activeConnection connection timeout (#93454)', () => {
  afterEach(() => {
    setApiRequestConnection(null)
    setApiRequestProfile(null)
    Reflect.deleteProperty(window, 'hermesDesktop')
    vi.useRealTimers()
  })

  it('rejects instead of hanging forever when getConnection() wedges', async () => {
    vi.useFakeTimers()
    setApiRequestProfile('coder')
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { getConnection: vi.fn(() => new Promise(() => undefined)) }
    })

    const pending = expect(activeConnection()).rejects.toThrow('Timed out connecting to profile "coder"')

    await vi.advanceTimersByTimeAsync(20_000)
    await pending
  })

  it('rejects instead of hanging forever when getConnectionFor() wedges', async () => {
    vi.useFakeTimers()
    setApiRequestConnection('gw-tailscale')
    setApiRequestProfile('research')
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        getConnection: vi.fn(() => new Promise(() => undefined)),
        getConnectionFor: vi.fn(() => new Promise(() => undefined))
      }
    })

    const pending = expect(activeConnection()).rejects.toThrow('Timed out connecting to profile "research"')

    await vi.advanceTimersByTimeAsync(20_000)
    await pending
  })
})
