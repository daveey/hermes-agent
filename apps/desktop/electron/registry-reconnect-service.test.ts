import { expect, test, vi } from 'vitest'

import { ManagedConnectionUpdateGate } from './managed-ssh-update'
import { createRegistryReconnectService } from './registry-reconnect-service'

function fixture() {
  const descriptor = { baseUrl: 'http://hub', token: 'synthetic' }
  const primary = { connectionPromise: Promise.resolve({ baseUrl: 'http://local' }) }
  const entry = { connectionPromise: Promise.resolve(descriptor) }
  const state = { registryConnectionId: 'hub', primaryRegistryScope: false, spawnNonce: 'owned', remotePlatform: 'Linux' }
  const pool = new Map([['default', primary], ['conn:hub::default', entry]])
  const states = new Map([['conn:hub::default', state]])
  const gate = new ManagedConnectionUpdateGate()

  const request = vi.fn(async (_descriptor, path, options) => path === '/api/ssh/ownership'
    ? { ok: true, protocolVersion: 1, sshOwnerNonce: 'owned', runtimeIntact: false }
    : { ok: true, idle: true, token: 'permit', action: options.body?.action })

  const terminate = vi.fn(async () => {})
  const close = vi.fn(async () => {})

  const reopen = vi.fn(async (id, profile, correlation) => {
    gate.assertCanDial(id, correlation)
    pool.set(`conn:${id}::${profile}`, { connectionPromise: Promise.resolve(descriptor) })
    states.set(`conn:${id}::${profile}`, { ...state, spawnNonce: 'new-owned' })
  })

  const service = createRegistryReconnectService({
    pool, states, gate, request, terminate, close, reopen,
    onCleanupError: vi.fn(),
    key: (id, profile) => `conn:${id}::${profile}`,
    isSsh: id => id === 'hub',
    inFlight: () => false
  })

  return { service, pool, states, entry, state, primary, gate, request, terminate, reopen, close }
}

test('composed reconnect repairs a deleted runtime without touching local default', async () => {
  const f = fixture()
  const target = f.service.target('hub', 'default')!
  expect(target.generation).toBeTruthy()
  const result = await f.service.run(target)
  expect(result.ok).toBe(true)
  expect(result.generation).not.toBe(target.generation)
  expect(f.terminate).toHaveBeenCalledWith(f.state)
  expect(f.pool.get('default')).toBe(f.primary)
  expect(f.request.mock.calls.map(call => call[2].body?.action).filter(Boolean)).toEqual(['prepare', 'commit'])
  expect(f.gate.owner('hub')).toBeNull()
})

test('a second profile cannot borrow a pending reconnect claim', async () => {
  const f = fixture()
  f.pool.set('conn:hub::other', { connectionPromise: f.entry.connectionPromise })
  f.states.set('conn:hub::other', { ...f.state })
  const first = f.service.target('hub', 'default')!
  const second = f.service.target('hub', 'other')!
  let finish!: () => void
  f.terminate.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve }))
  const running = f.service.run(first)
  await vi.waitFor(() => expect(f.terminate).toHaveBeenCalledOnce())
  expect(await f.service.run(second)).toEqual({ ok: false })
  expect(() => f.gate.assertCanDial('hub')).toThrow()
  finish()
  await running
})

test('a primary alias cannot acquire retirement authority', async () => {
  const f = fixture()
  f.state.primaryRegistryScope = true
  expect(f.service.target('hub', 'default')).toBeNull()
  expect(await f.service.run({ connectionId: 'hub', profile: 'default', generation: 'invented' })).toEqual({ ok: false })
  expect(f.request).not.toHaveBeenCalled()
  expect(f.terminate).not.toHaveBeenCalled()
})

test('busy backend releases the connection gate without tearing down its transport', async () => {
  const f = fixture()
  const target = f.service.target('hub', 'default')!
  f.request.mockImplementation(async (_d, path) => path === '/api/ssh/ownership'
    ? { ok: true, protocolVersion: 1, sshOwnerNonce: 'owned' } : { ok: false, idle: false })
  expect(await f.service.run(target)).toEqual({ ok: false })
  expect(f.terminate).not.toHaveBeenCalled()
  expect(f.close).not.toHaveBeenCalled()
  expect(f.gate.owner('hub')).toBeNull()
})

test('unsupported termination platform is refused before retirement', async () => {
  const f = fixture()
  const target = f.service.target('hub', 'default')!
  f.state.remotePlatform = 'Darwin'
  expect(await f.service.run(target)).toEqual({ ok: false })
  expect(f.request).not.toHaveBeenCalled()
  expect(f.terminate).not.toHaveBeenCalled()
  expect(f.gate.owner('hub')).toBeNull()
})

test('descriptor replacement invalidates a previously discovered generation', async () => {
  const f = fixture()
  const target = f.service.target('hub', 'default')!
  f.entry.connectionPromise = Promise.resolve({ baseUrl: 'http://replacement', token: 'replacement' })
  expect(f.service.target('hub', 'default')!.generation).not.toBe(target.generation)
  expect(await f.service.run(target)).toEqual({ ok: false })
  expect(f.request).not.toHaveBeenCalled()
  expect(f.terminate).not.toHaveBeenCalled()
})

test('transport cleanup failure after confirmed exit does not strand admission', async () => {
  const f = fixture()
  const target = f.service.target('hub', 'default')!
  f.close.mockRejectedValueOnce(new Error('transport already closed'))
  expect((await f.service.run(target)).ok).toBe(true)
  expect(f.reopen).toHaveBeenCalledOnce()
  expect(f.gate.owner('hub')).toBeNull()
})

test('ambiguous termination retains both gates and exact-generation retry can recover', async () => {
  const f = fixture()
  const target = f.service.target('hub', 'default')!
  f.terminate.mockRejectedValueOnce(new Error('lost transport'))
  await expect(f.service.run(target)).rejects.toThrow('lost transport')
  expect(() => f.gate.assertCanDial('hub')).toThrow()
  expect(() => f.service.assertCanStop('conn:hub::default')).toThrow()
  expect(() => f.service.assertCanStop('default')).not.toThrow()
  expect(f.close).not.toHaveBeenCalled()
  expect((await f.service.run(target)).ok).toBe(true)
  expect(f.gate.owner('hub')).toBeNull()
})
