import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it, vi } from 'vitest'

import { coalescedBackendRead, ReadCoalescer } from './read-coalescing'

const deferred = <T>() => {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void

  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })

  return { promise, reject, resolve }
}

const hub = { baseUrl: 'http://127.0.0.1:59853', authMode: 'token', token: 'tok-a', headers: { 'X-A': '1' } }
const LIST = '/api/profiles/sessions?limit=50&profile=all&source=cron'

describe('coalescedBackendRead', () => {
  it('sends one request for identical concurrent reads and gives each caller its own copy', async () => {
    const coalescer = new ReadCoalescer()
    const answer = deferred<{ sessions: { id: string }[] }>()
    const once = vi.fn(() => answer.promise)

    const reads = Array.from({ length: 10 }, () =>
      coalescedBackendRead(coalescer, hub, LIST, { timeoutMs: 15_000 }, once)
    )

    answer.resolve({ sessions: [{ id: 'a' }] })
    const results = await Promise.all(reads)

    expect(once).toHaveBeenCalledTimes(1)
    expect(results.every(result => result.sessions[0].id === 'a')).toBe(true)

    // A caller that edits its answer (tagRemoteSessionRows does) cannot change another caller's.
    results[0].sessions[0].id = 'edited'
    expect(results.slice(1).every(result => result.sessions[0].id === 'a')).toBe(true)
    expect(new Set(results).size).toBe(10)
  })

  it('asks again once the shared read has settled', async () => {
    const coalescer = new ReadCoalescer()
    const once = vi.fn(async () => ({ n: once.mock.calls.length }))

    const first = await coalescedBackendRead(coalescer, hub, LIST, {}, once)
    const second = await coalescedBackendRead(coalescer, hub, LIST, {}, once)

    expect(once).toHaveBeenCalledTimes(2)
    expect([first.n, second.n]).toEqual([1, 2])
    expect(coalescer.inflight).toBe(0)
  })

  it('keeps reads apart when anything on the wire or the deadline differs', async () => {
    const coalescer = new ReadCoalescer()
    const gate = deferred<unknown>()
    const once = vi.fn(() => gate.promise)

    const variants = [
      coalescedBackendRead(coalescer, hub, LIST, {}, once),
      coalescedBackendRead(coalescer, hub, `${LIST}&offset=50`, {}, once),
      coalescedBackendRead(coalescer, { ...hub, token: 'tok-b' }, LIST, {}, once),
      coalescedBackendRead(coalescer, { ...hub, baseUrl: 'http://127.0.0.1:60000' }, LIST, {}, once),
      coalescedBackendRead(coalescer, { ...hub, headers: { 'X-A': '2' } }, LIST, {}, once),
      coalescedBackendRead(coalescer, hub, LIST, { timeoutMs: 4_000 }, once),
      coalescedBackendRead(coalescer, hub, LIST, { pool: 'probe' }, once),
      coalescedBackendRead(coalescer, hub, LIST, { profileScoped: true }, once)
    ]

    gate.resolve({})
    await Promise.all(variants)
    expect(once).toHaveBeenCalledTimes(variants.length)
  })

  it('never coalesces writes, and a read after a write does not join a read sent before it', async () => {
    const coalescer = new ReadCoalescer()
    const before = deferred<string>()
    const after = deferred<string>()
    const reads = [before, after]

    const once = vi.fn((_d: unknown, _p: string, opts: { method?: string; body?: unknown }) =>
      opts.method === 'POST' ? Promise.resolve('wrote') : (reads.shift() as ReturnType<typeof deferred<string>>).promise
    )

    const stale = coalescedBackendRead(coalescer, hub, LIST, {}, once)

    const writes = [
      coalescedBackendRead(coalescer, hub, '/api/sessions/x/archive', { method: 'POST', body: {} }, once),
      coalescedBackendRead(coalescer, hub, '/api/sessions/x/archive', { method: 'POST', body: {} }, once)
    ]

    const fresh = coalescedBackendRead(coalescer, hub, LIST, {}, once)

    before.resolve('before the write')
    after.resolve('after the write')

    expect(await Promise.all(writes)).toEqual(['wrote', 'wrote'])
    expect(await stale).toBe('before the write')
    expect(await fresh).toBe('after the write')
    expect(once).toHaveBeenCalledTimes(4)
  })

  it('keeps reads apart for multiplexed native grants of different profiles on one api_origin', async () => {
    const coalescer = new ReadCoalescer()

    const once = vi.fn(
      async (descriptor: { gatewayEndpoint?: Record<string, unknown> }) =>
        descriptor.gatewayEndpoint?.profile_id ?? 'none'
    )

    const grant = {
      profile_id: '/h/.hermes',
      instance_id: 'i-1',
      authority_epoch: 3,
      api_origin: 'http://127.0.0.1:9147'
    }

    const native = { ...hub, authMode: 'native', gatewayEndpoint: grant }

    const reads = [
      coalescedBackendRead(coalescer, native, '/api/sessions', {}, once),
      coalescedBackendRead(
        coalescer,
        { ...native, gatewayEndpoint: { ...grant, profile_id: '/h/.hermes/profiles/b' } },
        '/api/sessions',
        {},
        once
      ),
      coalescedBackendRead(
        coalescer,
        { ...native, gatewayEndpoint: { ...grant, instance_id: 'i-2' } },
        '/api/sessions',
        {},
        once
      ),
      coalescedBackendRead(
        coalescer,
        { ...native, gatewayEndpoint: { ...grant, authority_epoch: 4 } },
        '/api/sessions',
        {},
        once
      ),
      coalescedBackendRead(coalescer, { ...native, gatewayEndpoint: undefined }, '/api/sessions', {}, once),
      // The same grant (a fresh descriptor object for it) still shares.
      coalescedBackendRead(coalescer, { ...native, gatewayEndpoint: { ...grant } }, '/api/sessions', {}, once)
    ]

    expect(await Promise.all(reads)).toEqual([
      '/h/.hermes',
      '/h/.hermes/profiles/b',
      '/h/.hermes',
      '/h/.hermes',
      'none',
      '/h/.hermes'
    ])
    expect(once).toHaveBeenCalledTimes(5)
  })

  it('a read issued after a write settles never joins one sent while the write was in flight', async () => {
    const coalescer = new ReadCoalescer()
    const write = deferred<string>()
    const during = deferred<string>()
    const after = deferred<string>()
    const reads = [during, after]

    const once = vi.fn((_d: unknown, _p: string, opts: { method?: string; body?: unknown }) =>
      opts.method === 'POST' ? write.promise : (reads.shift() as ReturnType<typeof deferred<string>>).promise
    )

    const writing = coalescedBackendRead(coalescer, hub, '/api/sessions/x/archive', { method: 'POST', body: {} }, once)
    const racing = coalescedBackendRead(coalescer, hub, LIST, {}, once) // may be answered before the write lands

    write.resolve('wrote')
    await writing
    const fresh = coalescedBackendRead(coalescer, hub, LIST, {}, once)

    during.resolve('while writing')
    after.resolve('after the write')
    expect(await racing).toBe('while writing')
    expect(await fresh).toBe('after the write')
    expect(once).toHaveBeenCalledTimes(3)
  })

  it('a write announced from elsewhere (a WS-RPC session mutation) starts a new generation on every backend', async () => {
    const coalescer = new ReadCoalescer()
    const gates = [deferred<string>(), deferred<string>(), deferred<string>()]
    const queue = [...gates]
    const once = vi.fn(() => (queue.shift() as ReturnType<typeof deferred<string>>).promise)
    const other = { ...hub, baseUrl: 'http://127.0.0.1:60000' }

    const stale = coalescedBackendRead(coalescer, hub, LIST, {}, once)
    const staleOther = coalescedBackendRead(coalescer, other, LIST, {}, once)
    coalescer.noteWriteEverywhere()
    const fresh = coalescedBackendRead(coalescer, hub, LIST, {}, once)
    // Unchanged by the notice: reads issued after it still share with each other.
    const freshJoin = coalescedBackendRead(coalescer, hub, LIST, {}, once)

    gates.forEach((gate, n) => gate.resolve(`answer ${n}`))
    expect(await Promise.all([stale, staleOther, fresh, freshJoin])).toEqual([
      'answer 0',
      'answer 1',
      'answer 2',
      'answer 2'
    ])
    expect(once).toHaveBeenCalledTimes(3)
  })

  it('shares a failure with every caller and then lets the next read try again', async () => {
    const coalescer = new ReadCoalescer()
    const answer = deferred<unknown>()
    const once = vi.fn(() => answer.promise)

    const reads = [1, 2, 3].map(() => coalescedBackendRead(coalescer, hub, LIST, {}, once))
    answer.reject(new Error('Timed out connecting to Hermes backend after 15000ms'))

    for (const read of reads) {
      await expect(read).rejects.toThrow('Timed out')
    }

    expect(once).toHaveBeenCalledTimes(1)
    await coalescedBackendRead(
      coalescer,
      hub,
      LIST,
      {},
      vi.fn(async () => 'ok')
    )
    expect(coalescer.inflight).toBe(0)
  })

  it('treats only bodyless GETs as reads', () => {
    expect(ReadCoalescer.isRead({})).toBe(true)
    expect(ReadCoalescer.isRead({ method: 'get' })).toBe(true)
    expect(ReadCoalescer.isRead({ method: 'GET', body: {} })).toBe(false)
    expect(ReadCoalescer.isRead({ method: 'GET', upload: { path: '/x' } })).toBe(false)
    expect(ReadCoalescer.isRead({ method: 'DELETE' })).toBe(false)
  })
})

describe('main.ts wiring', () => {
  const main = readFileSync(join(__dirname, 'main.ts'), 'utf8')

  it('sends every backend REST call through the coalescer, and the coalescer only to the real transport', () => {
    const front = main.slice(
      main.indexOf('function fetchJsonForBackend('),
      main.indexOf('async function fetchJsonForBackendOnce(')
    )

    expect(front).toContain(
      'coalescedBackendRead(backendReadCoalescer, descriptor, path, opts, fetchJsonForBackendOnce)'
    )
    // Nothing else calls the uncoalesced transport, so no read can bypass the sharing.
    expect(main.match(/\bfetchJsonForBackendOnce\b/g)).toHaveLength(2)
  })

})
