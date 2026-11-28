import { expect, test, vi } from 'vitest'

import { createRegistryReconnector } from './registry-reconnect'

function fixture() {
  let entry = { generation: 'hub-1' }

  const deps = {
    current: vi.fn(() => entry),
    generation: (value: typeof entry) => value.generation,
    proveOwned: vi.fn(async () => true),
    prepare: vi.fn(async (): Promise<string | null> => 'permit'),
    commit: vi.fn(async () => true),
    cancel: vi.fn(async () => {}),
    recoverStopped: vi.fn(async () => false),
    terminate: vi.fn(async () => {}),
    reopen: vi.fn(async () => 'hub-2')
  }

  return {
    deps,
    reconnect: createRegistryReconnector(deps),
    replace: () => {
      entry = { generation: 'hub-new' }
    }
  }
}

const target = { connectionId: 'hub', profile: 'default', generation: 'hub-1' }

test.each([
  { connectionId: '', profile: 'default', generation: 'hub-1' },
  { connectionId: 'hub', profile: '', generation: 'hub-1' },
  { connectionId: 'hub', profile: 'default', generation: '' },
  { connectionId: ' hub', profile: 'default', generation: 'hub-1' }
])('refuses incomplete or implicitly normalized target %j', async invalid => {
  const { deps, reconnect } = fixture()
  expect(await reconnect.run(invalid)).toEqual({ ok: false })
  expect(deps.proveOwned).not.toHaveBeenCalled()
  expect(deps.prepare).not.toHaveBeenCalled()
})

test.each(['busy', 'unowned', 'stale'] as const)('fails closed for %s without terminating', async refusal => {
  const { deps, reconnect } = fixture()

  if (refusal === 'busy') {
    deps.prepare.mockResolvedValue(null)
  }

  if (refusal === 'unowned') {
    deps.proveOwned.mockResolvedValue(false)
  }
  expect(await reconnect.run({ ...target, generation: refusal === 'stale' ? 'old' : target.generation })).toEqual({
    ok: false
  })
  expect(deps.terminate).not.toHaveBeenCalled()
  expect(deps.reopen).not.toHaveBeenCalled()
  expect(() => reconnect.assertCanDial('hub', 'default')).not.toThrow()
})

test('a refused commit cancels preparation and releases the local dial gate', async () => {
  const { deps, reconnect } = fixture()
  deps.commit.mockResolvedValue(false)
  expect(await reconnect.run(target)).toEqual({ ok: false })
  expect(deps.cancel).toHaveBeenCalledWith(expect.anything(), 'permit')
  expect(deps.terminate).not.toHaveBeenCalled()
  expect(() => reconnect.assertCanDial('hub', 'default')).not.toThrow()
})

test('termination failure retains the committed fence for an exact-generation retry', async () => {
  const { deps, reconnect } = fixture()
  deps.terminate.mockRejectedValueOnce(new Error('transport lost'))
  await expect(reconnect.run(target)).rejects.toThrow('transport lost')
  expect(deps.cancel).not.toHaveBeenCalled()
  expect(deps.reopen).not.toHaveBeenCalled()
  expect(() => reconnect.assertCanDial('hub', 'default')).toThrow('reconnect')
  // The signal may have succeeded before its response was lost. Recovery
  // must use exact SSH process identity, not the now-dead HTTP server.
  deps.proveOwned.mockRejectedValue(new Error('serve already stopped'))
  deps.prepare.mockRejectedValue(new Error('serve already stopped'))
  expect(await reconnect.run(target)).toEqual({ ok: true, generation: 'hub-2' })
})

test('a replacement during prepare cancels only the old permit without committing or terminating', async () => {
  const { deps, reconnect, replace } = fixture()
  deps.prepare.mockImplementation(async () => {
    replace()

    return 'permit'
  })
  expect(await reconnect.run(target)).toEqual({ ok: false })
  expect(deps.cancel).toHaveBeenCalledOnce()
  expect(deps.commit).not.toHaveBeenCalled()
  expect(deps.terminate).not.toHaveBeenCalled()
})

test('freezes same-scope dials and duplicate reconnects before the first async ownership probe', async () => {
  const { deps, reconnect } = fixture()
  let resolve!: (value: boolean) => void
  deps.proveOwned.mockImplementation(
    () =>
      new Promise<boolean>(done => {
        resolve = done
      })
  )
  const first = reconnect.run(target)
  expect(() => reconnect.assertCanDial('hub', 'default')).toThrow('reconnect')
  expect(() => reconnect.assertCanDial('hub', 'reviewer')).not.toThrow()
  expect(() => reconnect.assertCanDial('other', 'default')).not.toThrow()
  expect(await reconnect.run(target)).toEqual({ ok: false })
  resolve(false)
  expect(await first).toEqual({ ok: false })
  expect(() => reconnect.assertCanDial('hub', 'default')).not.toThrow()
  expect(deps.terminate).not.toHaveBeenCalled()
})

test('an ambiguous commit keeps the scope fenced for retry, never cancelling a possibly committed permit', async () => {
  const { deps, reconnect } = fixture()
  deps.commit.mockRejectedValueOnce(new Error('response lost'))
  await expect(reconnect.run(target)).rejects.toThrow('response lost')
  expect(deps.cancel).not.toHaveBeenCalled()
  expect(deps.terminate).not.toHaveBeenCalled()
  expect(() => reconnect.assertCanDial('hub', 'default')).toThrow('reconnect')
  expect(await reconnect.run(target)).toEqual({ ok: true, generation: 'hub-2' })
  expect(() => reconnect.assertCanDial('hub', 'default')).not.toThrow()
})

test('lost commit plus serve exit recovers without invoking any live terminator', async () => {
  const { deps, reconnect } = fixture()
  deps.commit.mockRejectedValueOnce(new Error('response lost'))
  await expect(reconnect.run(target)).rejects.toThrow('response lost')
  deps.proveOwned.mockRejectedValue(new Error('HTTP offline'))
  deps.recoverStopped.mockResolvedValue(true)
  expect(await reconnect.run(target)).toEqual({ ok: true, generation: 'hub-2' })
  expect(deps.terminate).not.toHaveBeenCalled()
  expect(() => reconnect.assertCanDial('hub', 'default')).not.toThrow()
})

test.each(['live', 'unknown'] as const)('uncertain %s owner never grants kill authority', async status => {
  const { deps, reconnect } = fixture()
  deps.commit.mockRejectedValueOnce(new Error('response lost'))
  await expect(reconnect.run(target)).rejects.toThrow('response lost')
  deps.proveOwned.mockRejectedValue(new Error('HTTP offline'))

  if (status === 'unknown') {
    deps.recoverStopped.mockRejectedValue(new Error('SSH offline'))
  }
  await expect(reconnect.run(target)).rejects.toThrow(/offline/)
  expect(deps.terminate).not.toHaveBeenCalled()
  expect(deps.reopen).not.toHaveBeenCalled()
  expect(() => reconnect.assertCanDial('hub', 'default')).toThrow()
})

test('an uncertain committed resident cannot grant retry authority over a replacement', async () => {
  const { deps, reconnect, replace } = fixture()
  deps.commit.mockRejectedValueOnce(new Error('response lost'))
  await expect(reconnect.run(target)).rejects.toThrow('response lost')
  replace()
  expect(await reconnect.run({ ...target, generation: 'hub-new' })).toEqual({ ok: false })
  expect(deps.prepare).toHaveBeenCalledOnce()
  expect(deps.terminate).not.toHaveBeenCalled()
  expect(() => reconnect.assertCanDial('hub', 'default')).toThrow('reconnect')
})

test('replacement during commit cannot terminate or reopen a newer resident', async () => {
  const { deps, reconnect, replace } = fixture()
  deps.commit.mockImplementation(async () => {
    replace()

    return true
  })
  expect(await reconnect.run(target)).toEqual({ ok: false })
  expect(deps.terminate).not.toHaveBeenCalled()
  expect(deps.reopen).not.toHaveBeenCalled()
  expect(() => reconnect.assertCanDial('hub', 'default')).toThrow('reconnect')
})

test('replacement while termination is pending cannot be reopened by the old request', async () => {
  const { deps, reconnect, replace } = fixture()
  deps.terminate.mockImplementation(async () => {
    replace()
  })
  expect(await reconnect.run(target)).toEqual({ ok: false })
  expect(deps.reopen).not.toHaveBeenCalled()
  expect(() => reconnect.assertCanDial('hub', 'default')).not.toThrow()
})

test('reconnects only the generation-qualified registry default, never the primary default', async () => {
  const primary = { generation: 'primary' }
  const target = { generation: 'hub-1' }
  const other = { generation: 'other-1' }
  const entries = new Map([
    ['default', primary],
    ['conn:hub::default', target],
    ['conn:other::default', other]
  ])
  const prepare = vi.fn(async () => 'permit')
  const cancel = vi.fn(async () => {})
  const commit = vi.fn(async () => true)

  const terminate = vi.fn(async (entry: typeof target) => {
    expect(entry).toBe(target)
  })

  const reopen = vi.fn(async () => 'hub-2')

  const reconnect = createRegistryReconnector({
    current: (connectionId: string, profile: string) => entries.get(`conn:${connectionId}::${profile}`),
    generation: (entry: typeof target) => entry.generation,
    proveOwned: async () => true,
    recoverStopped: async () => false,
    prepare,
    commit,
    cancel,
    terminate,
    reopen
  })

  expect(await reconnect.run({ connectionId: 'hub', profile: 'default', generation: 'hub-1' })).toEqual({
    ok: true,
    generation: 'hub-2'
  })
  expect(prepare).toHaveBeenCalledWith(target)
  expect(commit).toHaveBeenCalledWith(target, 'permit')
  expect(terminate).toHaveBeenCalledOnce()
  expect(reopen).toHaveBeenCalledWith('hub', 'default', target)
  expect(cancel).not.toHaveBeenCalled()
  expect(entries.get('default')).toBe(primary)
  expect(entries.get('conn:other::default')).toBe(other)
})
