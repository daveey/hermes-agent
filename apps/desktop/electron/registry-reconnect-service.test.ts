import { expect, test, vi } from 'vitest'

import { ManagedConnectionUpdateGate } from './managed-ssh-update'
import { createRegistryReconnectService } from './registry-reconnect-service'
import type { createRegistryRetirementClient } from './registry-retirement-http'

function fixture() {
  const descriptor = { baseUrl: 'http://hub', token: 'synthetic' }
  const primary = {
    process: null,
    connectionPromise: Promise.resolve({ baseUrl: 'http://local', token: 'synthetic-local' })
  }
  const entry = { process: null, connectionPromise: Promise.resolve(descriptor) }
  const state = {
    registryConnectionId: 'hub',
    primaryRegistryScope: false,
    spawnNonce: 'owned',
    remotePlatform: 'Linux'
  }
  const pool = new Map([
    ['default', primary],
    ['conn:hub::default', entry]
  ])
  const states = new Map([['conn:hub::default', state]])
  const gate = new ManagedConnectionUpdateGate()

  const request = vi.fn<Parameters<typeof createRegistryRetirementClient<typeof descriptor>>[0]>(
    async (_descriptor, path, options) =>
      path === '/api/ssh/ownership'
        ? { ok: true, protocolVersion: 1, sshOwnerNonce: 'owned', runtimeIntact: false }
        : { ok: true, idle: true, token: 'permit', action: options.body?.action }
  )

  const terminate = vi.fn(async () => {})
  const proveStopped = vi.fn(async () => false)
  const close = vi.fn(async () => {})

  const reopen = vi.fn(async (id, profile, correlation) => {
    gate.assertCanDial(id, correlation)
    pool.set(`conn:${id}::${profile}`, { process: null, connectionPromise: Promise.resolve(descriptor) })
    states.set(`conn:${id}::${profile}`, { ...state, spawnNonce: 'new-owned' })
  })

  const service = createRegistryReconnectService({
    pool,
    states,
    gate,
    request,
    terminate,
    close,
    reopen,
    proveStopped,
    onCleanupError: vi.fn(),
    key: (id, profile) => `conn:${id}::${profile}`,
    isSsh: id => id === 'hub',
    inFlight: () => false
  })

  return { service, pool, states, entry, state, primary, gate, request, terminate, reopen, close, proveStopped }
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

test.each(['stopped', 'replacement', 'live', 'unknown'] as const)('ambiguous commit recovery: %s', async verdict => {
  const f = fixture()
  const target = f.service.target('hub', 'default')!
  const request = f.request.getMockImplementation()!
  f.request.mockImplementation(async (descriptor, path, options) => {
    if (options.body?.action === 'commit') {
      throw new Error('response lost')
    }

    return request(descriptor, path, options)
  })
  await expect(f.service.run(target)).rejects.toThrow('response lost')
  f.request.mockRejectedValue(new Error('HTTP offline'))
  f.proveStopped.mockImplementation(async () => {
    if (verdict === 'unknown') {
      throw new Error('SSH offline')
    }

    if (verdict === 'replacement') {
      f.entry.connectionPromise = Promise.resolve({ baseUrl: 'http://replacement', token: 'new' })
    }

    return verdict !== 'live'
  })

  if (verdict === 'stopped') {
    expect((await f.service.run(target)).ok).toBe(true)
    expect(f.close).toHaveBeenCalledOnce()
    expect(f.gate.owner('hub')).toBeNull()
  } else {
    await expect(f.service.run(target)).rejects.toThrow(/offline|changed/)
    expect(f.close).not.toHaveBeenCalled()
    expect(f.reopen).not.toHaveBeenCalled()
    expect(f.gate.owner('hub')).not.toBeNull()
    expect(f.pool.get('conn:hub::default')).toBe(f.entry)
  }

  expect(f.terminate).not.toHaveBeenCalled()
  expect(f.pool.get('default')).toBe(f.primary)
})

test('a second profile cannot borrow a pending reconnect claim', async () => {
  const f = fixture()
  f.pool.set('conn:hub::other', { process: null, connectionPromise: f.entry.connectionPromise })
  f.states.set('conn:hub::other', { ...f.state })
  const first = f.service.target('hub', 'default')!
  const second = f.service.target('hub', 'other')!
  let finish!: () => void
  f.terminate.mockImplementationOnce(
    () =>
      new Promise<void>(resolve => {
        finish = resolve
      })
  )
  const running = f.service.run(first)
  await vi.waitFor(() => expect(f.terminate).toHaveBeenCalledOnce())
  expect(await f.service.run(second)).toEqual({ ok: false })
  expect(() => f.gate.assertCanDial('hub')).toThrow()
  expect(() => f.service.assertCanDispatch('hub', 'other')).not.toThrow()
  expect(() => f.service.assertCanDispatch('hub', 'default')).toThrow()
  expect(() => f.service.assertCanStop('conn:hub::other')).not.toThrow()
  expect(() => f.service.assertCanStop('conn:hub::default')).toThrow()
  finish()
  await running
})

test('direct profile deletion cannot evict a resident during reconnect commit', async () => {
  const f = fixture()
  const target = f.service.target('hub', 'default')!
  let commit!: () => void
  const request = f.request.getMockImplementation()!
  f.request.mockImplementation(async (descriptor, path, options) => {
    if (options.body?.action === 'commit') {
      await new Promise<void>(resolve => {
        commit = resolve
      })
    }

    return request(descriptor, path, options)
  })

  const running = f.service.run(target)
  await vi.waitFor(() => expect(commit).toBeTypeOf('function'))
  // main.ts stopPoolBackend calls assertCanStop before its synchronous pool delete
  // (the fence pool-stop.ts used to carry), so a throw here means the resident stays.
  expect(() => f.service.assertCanStop('conn:hub::default')).toThrow('reconnect in progress')
  expect(f.pool.get('conn:hub::default')).toBe(f.entry)
  commit()
  expect((await running).ok).toBe(true)
  expect(f.gate.owner('hub')).toBeNull()
})

test('a primary alias cannot acquire retirement authority', async () => {
  const f = fixture()
  f.state.primaryRegistryScope = true
  expect(f.service.target('hub', 'default')).toBeNull()
  expect(await f.service.run({ connectionId: 'hub', profile: 'default', generation: 'invented' })).toEqual({
    ok: false
  })
  expect(f.request).not.toHaveBeenCalled()
  expect(f.terminate).not.toHaveBeenCalled()
})

test('busy backend releases the connection gate without tearing down its transport', async () => {
  const f = fixture()
  const target = f.service.target('hub', 'default')!
  f.request.mockImplementation(async (_d, path) =>
    path === '/api/ssh/ownership'
      ? { ok: true, protocolVersion: 1, sshOwnerNonce: 'owned' }
      : { ok: false, idle: false }
  )
  expect(await f.service.run(target)).toEqual({ ok: false })
  expect(f.terminate).not.toHaveBeenCalled()
  expect(f.close).not.toHaveBeenCalled()
  expect(f.gate.owner('hub')).toBeNull()
})

test.each(['Darwin', 'Windows'])('unsupported termination platform %s is refused before retirement', async platform => {
  const f = fixture()
  const target = f.service.target('hub', 'default')!
  f.state.remotePlatform = platform
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
