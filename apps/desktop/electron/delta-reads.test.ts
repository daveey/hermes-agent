import http from 'node:http'
import type { AddressInfo } from 'node:net'
import { gzipSync, zstdCompressSync } from 'node:zlib'

import { afterAll, describe, expect, it } from 'vitest'

import { destroyKeepaliveAgents, shouldRetryRequest, withRetry } from './api-transport'
import {
  bodyHash,
  decodeDeltaBody,
  DELTA_ACCEPT_HEADER,
  DELTA_BASE_HEADER,
  DELTA_RETRY_CODE,
  DELTA_STORE_MAX_BYTES,
  DELTA_STORE_MAX_ENTRIES,
  DeltaBodyStore,
  deltaBodyStore,
  deltaRequestHeaders,
  DeltaRetryError,
  readEncodingHeaders
} from './delta-reads'
import { fetchJsonWith, type JsonFetchDeps } from './json-fetch'
import { noteRemoteConnect, slowLinkTracker } from './slow-link'

const HUB = 'http://127.0.0.1:49882'
const SCOPE = 'conn:hermes-hub::default'
const BOARD = `${HUB}/api/plugins/kanban/board?board=default`

const cards = (count: number, done = -1) =>
  Buffer.from(JSON.stringify({ cards: Array.from({ length: count }, (_, id) => ({ id, status: id === done ? 'done' : 'todo', title: `card ${id}` })) }))

const whole = (body: Buffer, extra: Record<string, string> = {}) => ({
  headers: { 'content-type': 'application/json', 'x-hermes-body-hash': bodyHash(body), ...extra },
  statusCode: 200
})

const delta = (base: Buffer, next: Buffer, over: Record<string, string> = {}) => ({
  body: zstdCompressSync(next, { dictionary: base }),
  response: {
    headers: {
      'content-type': 'application/vnd.hermes.delta+zstd',
      'x-hermes-body-hash': bodyHash(next),
      'x-hermes-delta': 'zstd',
      'x-hermes-delta-base': bodyHash(base),
      ...over
    },
    statusCode: 200
  }
})

function bound() {
  const store = new DeltaBodyStore()

  store.bindRemote(`${HUB}/`, SCOPE)

  return store
}

describe('asking for a delta', () => {
  it('asks only on a GET to a bound remote backend, and names the base once a copy is held', () => {
    const store = bound()

    expect(deltaRequestHeaders('GET', BOARD, store)).toEqual({ [DELTA_ACCEPT_HEADER]: 'zstd' })
    expect(deltaRequestHeaders(undefined, BOARD, store)).toEqual({ [DELTA_ACCEPT_HEADER]: 'zstd' })
    expect(deltaRequestHeaders('POST', BOARD, store)).toEqual({})
    // A local backend, or a remote one that is not SSH-tunnelled, is never asked.
    expect(deltaRequestHeaders('GET', 'http://127.0.0.1:9119/api/plugins/kanban/board', store)).toEqual({})
    expect(deltaRequestHeaders('GET', 'not a url', store)).toEqual({})

    const first = cards(40)

    decodeDeltaBody(BOARD, whole(first), first, store)
    expect(deltaRequestHeaders('GET', BOARD, store)).toEqual({ [DELTA_ACCEPT_HEADER]: 'zstd', [DELTA_BASE_HEADER]: bodyHash(first) })
    // Another query is another read.
    expect(deltaRequestHeaders('GET', `${HUB}/api/plugins/kanban/board?board=runners`, store)).toEqual({ [DELTA_ACCEPT_HEADER]: 'zstd' })
  })

  it('a transcript page read keeps its own slim+gzip ask and does not also ask for a delta', () => {
    const store = bound()
    const transcript = `${HUB}/api/sessions/20261112_185653_5ff5dc/messages?limit=120&order=latest`

    expect(readEncodingHeaders('GET', transcript, store)).toMatchObject({ 'Accept-Encoding': 'gzip' })
    expect(readEncodingHeaders('GET', transcript, store)).not.toHaveProperty(DELTA_ACCEPT_HEADER)
    expect(readEncodingHeaders('GET', BOARD, store)).toEqual({ [DELTA_ACCEPT_HEADER]: 'zstd' })
  })

  it('a torn-down tunnel stops being asked: its port may be reused by a backend that is not this scope', () => {
    const store = bound()
    const body = cards(40)

    decodeDeltaBody(BOARD, whole(body), body, store)
    store.unbindScope(SCOPE)

    expect(deltaRequestHeaders('GET', BOARD, store)).toEqual({})
    // The copies wait for the rebuilt tunnel.
    store.bindRemote('http://127.0.0.1:61511', SCOPE)
    expect(deltaRequestHeaders('GET', 'http://127.0.0.1:61511/api/plugins/kanban/board?board=default', store)[DELTA_BASE_HEADER]).toBe(bodyHash(body))
  })

  it('keeps one origin per scope: a rebind without a teardown stops the old port being asked or filed under it', () => {
    const store = bound()
    const body = cards(40)
    const rebuilt = 'http://127.0.0.1:61511/api/plugins/kanban/board?board=default'

    decodeDeltaBody(BOARD, whole(body), body, store)
    store.bindRemote('http://127.0.0.1:61511', SCOPE) // the tunnel was rebuilt on a new port, no unbindScope

    // The old port: no ask, no key, and a hashed answer from whatever listens there now is not kept.
    expect(deltaRequestHeaders('GET', BOARD, store)).toEqual({})
    expect(store.keyFor(BOARD)).toBeNull()

    const stranger = cards(41)

    expect(decodeDeltaBody(BOARD, whole(stranger), stranger, store)).toBe(stranger.toString('utf8'))
    expect(store.size).toBe(1)
    // The scope and its copy moved to the new origin.
    expect(deltaRequestHeaders('GET', rebuilt, store)[DELTA_BASE_HEADER]).toBe(bodyHash(body))

    // Another scope's binding is untouched, and binding the same origin again changes nothing.
    store.bindRemote('http://127.0.0.1:50999', 'conn:hermes-hub::alpha')
    store.bindRemote('http://127.0.0.1:61511', SCOPE)
    expect(store.keyFor('http://127.0.0.1:50999/api/cron/jobs')).toBe('conn:hermes-hub::alpha\u0000/api/cron/jobs')
    expect(deltaRequestHeaders('GET', rebuilt, store)[DELTA_BASE_HEADER]).toBe(bodyHash(body))
  })

  it('dropping a pooled backend (slowLinkTracker.forgetScope) unbinds its tunnel in the shared store', () => {
    const url = 'http://127.0.0.1:50888/api/cron/jobs'

    noteRemoteConnect('http://127.0.0.1:50888', 'conn:hermes-hub::alpha', Date.now(), () => undefined)
    expect(deltaRequestHeaders('GET', url)).toEqual({ [DELTA_ACCEPT_HEADER]: 'zstd' })

    slowLinkTracker.forgetScope('conn:hermes-hub::alpha')
    expect(deltaRequestHeaders('GET', url)).toEqual({})
  })

  it('noteRemoteConnect binds a tunnel to its scope in the shared store', () => {
    const url = 'http://127.0.0.1:50777/api/cron/jobs'

    expect(deltaRequestHeaders('GET', url)).toEqual({})
    noteRemoteConnect('http://127.0.0.1:50777', 'conn:mac-mini::default', Date.now(), () => undefined)
    expect(deltaRequestHeaders('GET', url)).toEqual({ [DELTA_ACCEPT_HEADER]: 'zstd' })
    expect(deltaBodyStore.keyFor(url)).toBe('conn:mac-mini::default\u0000/api/cron/jobs')
  })
})

describe('decoding', () => {
  it('whole body, then a delta against it, then a delta against that: each returns the exact current body', () => {
    const store = bound()
    const v1 = cards(4000)
    const v2 = cards(4001, 1234)
    const v3 = cards(4001, 77)

    expect(decodeDeltaBody(BOARD, whole(v1), v1, store)).toBe(v1.toString('utf8'))

    const second = delta(v1, v2)

    expect(second.body.length).toBeLessThan(v2.length / 100)
    expect(decodeDeltaBody(BOARD, second.response, second.body, store)).toBe(v2.toString('utf8'))

    const third = delta(v2, v3)

    expect(decodeDeltaBody(BOARD, third.response, third.body, store)).toBe(v3.toString('utf8'))
    expect(deltaRequestHeaders('GET', BOARD, store)[DELTA_BASE_HEADER]).toBe(bodyHash(v3))
  })

  it('decodes a frame made by the gateway side (Python compression.zstd, prefix dictionary)', () => {
    const store = bound()
    const base = cards(40)
    const next = cards(41, 7)

    // Produced by: zstd.compress(new, level=3, zstd_dict=zstd.ZstdDict(old, is_raw=True).as_prefix), Python 3.14.
    const frame = Buffer.from('KLUv/WADBv0AAFh7ZG9uZTQwIn1dfQUAkoAY/AlfwBj4KUfbrIAL6go=', 'base64')

    expect(bodyHash(base)).toBe('534e8f50b28c0e95528daf1f36a43a0c')
    decodeDeltaBody(BOARD, whole(base), base, store)

    const response = {
      headers: {
        'x-hermes-body-hash': '611f65f648a6b3b611140694997e4b95',
        'x-hermes-delta': 'zstd',
        'x-hermes-delta-base': '534e8f50b28c0e95528daf1f36a43a0c'
      },
      statusCode: 200
    }

    expect(decodeDeltaBody(BOARD, response, frame, store)).toBe(next.toString('utf8'))
  })

  it('survives a tunnel rebuild: the copy is per scope and path, not per local port', () => {
    const store = bound()
    const v1 = cards(400)
    const v2 = cards(401)

    decodeDeltaBody(BOARD, whole(v1), v1, store)
    store.bindRemote('http://127.0.0.1:61511', SCOPE) // the rebuilt tunnel

    const rebuilt = 'http://127.0.0.1:61511/api/plugins/kanban/board?board=default'

    expect(deltaRequestHeaders('GET', rebuilt, store)[DELTA_BASE_HEADER]).toBe(bodyHash(v1))

    const next = delta(v1, v2)

    expect(decodeDeltaBody(rebuilt, next.response, next.body, store)).toBe(v2.toString('utf8'))
  })

  it.each([
    ['the base is not held', (b: Buffer, n: Buffer) => delta(b, n), false],
    ['it names another base', (b: Buffer, n: Buffer) => delta(b, n, { 'x-hermes-delta-base': 'f'.repeat(32) }), true],
    ['the decoded body does not match its hash', (b: Buffer, n: Buffer) => delta(b, n, { 'x-hermes-body-hash': '0'.repeat(32) }), true],
    ['the frame is corrupt', (b: Buffer, n: Buffer) => ({ ...delta(b, n), body: Buffer.from('not a zstd frame') }), true],
    ['it was encoded against other bytes', (b: Buffer, n: Buffer) => ({ ...delta(b, n), body: zstdCompressSync(n, { dictionary: cards(9) }) }), true]
  ])('fails for a retry and drops the copy when %s', (_name, make, seed) => {
    const store = bound()
    const base = cards(300)
    const next = cards(301, 5)

    if (seed) {
      decodeDeltaBody(BOARD, whole(base), base, store)
    }

    const bad = make(base, next)
    let thrown: unknown

    try {
      decodeDeltaBody(BOARD, bad.response, bad.body, store)
    } catch (error) {
      thrown = error
    }

    expect(thrown).toBeInstanceOf(DeltaRetryError)
    expect((thrown as DeltaRetryError).code).toBe(DELTA_RETRY_CODE)
    // The retry asks without a base, so the backend sends the whole body.
    expect(deltaRequestHeaders('GET', BOARD, store)).toEqual({ [DELTA_ACCEPT_HEADER]: 'zstd' })
    // api-transport retries it for a GET and never for a write.
    expect(shouldRetryRequest(thrown, 'GET', { bodySent: true })).toBe(true)
    expect(shouldRetryRequest(thrown, 'POST', { bodySent: true })).toBe(false)
  })

  it('two reads in flight on the same base: the second delta still decodes after the first replaced the copy', () => {
    const store = bound()
    const v0 = cards(3000)
    const v1 = cards(3001, 10)
    const v2 = cards(3001, 20)

    decodeDeltaBody(BOARD, whole(v0), v0, store)

    // Both requests were built while v0 was the copy, so both deltas name v0.
    expect(deltaRequestHeaders('GET', BOARD, store)[DELTA_BASE_HEADER]).toBe(bodyHash(v0))

    const first = delta(v0, v1)
    const second = delta(v0, v2)

    expect(decodeDeltaBody(BOARD, first.response, first.body, store)).toBe(v1.toString('utf8'))
    expect(decodeDeltaBody(BOARD, second.response, second.body, store)).toBe(v2.toString('utf8'))
    // The newest answer is the next base; the one before it is still held for a read already in flight.
    expect(deltaRequestHeaders('GET', BOARD, store)[DELTA_BASE_HEADER]).toBe(bodyHash(v2))
    expect(store.find(store.keyFor(BOARD)!, bodyHash(v1))).toBeDefined()
    expect(store.find(store.keyFor(BOARD)!, bodyHash(v0))).toBeUndefined()
  })

  it('a failed delta is retried by the transport without a base, and the caller gets the whole body', async () => {
    const store = bound()
    const base = cards(300)
    const current = cards(301, 5)

    decodeDeltaBody(BOARD, whole(base), base, store)

    const asked: Record<string, string>[] = []

    // One attempt as fetchJson makes it: headers from the store, then the response through decodeDeltaBody.
    const attempt = async () => {
      const headers = deltaRequestHeaders('GET', BOARD, store)

      asked.push(headers)

      if (headers[DELTA_BASE_HEADER]) {
        // The backend's frame is damaged in transit.
        return decodeDeltaBody(BOARD, delta(base, current).response, Buffer.from('damaged'), store)
      }

      return decodeDeltaBody(BOARD, whole(current), current, store)
    }

    await expect(withRetry(attempt, { delayFn: async () => undefined, method: 'GET' })).resolves.toBe(current.toString('utf8'))
    expect(asked).toEqual([{ [DELTA_ACCEPT_HEADER]: 'zstd', [DELTA_BASE_HEADER]: bodyHash(base) }, { [DELTA_ACCEPT_HEADER]: 'zstd' }])
  })

  it.each([
    ['no body hash', { 'x-hermes-body-hash': '' }, 200],
    ['a status other than 200', {}, 206]
  ])('a delta with %s also drops the copy, so the retry cannot fail the same way', (_name, over, statusCode) => {
    const store = bound()
    const base = cards(300)

    decodeDeltaBody(BOARD, whole(base), base, store)

    const bad = delta(base, cards(301), over)

    expect(() => decodeDeltaBody(BOARD, { ...bad.response, statusCode }, bad.body, store)).toThrow(DeltaRetryError)
    expect(deltaRequestHeaders('GET', BOARD, store)).toEqual({ [DELTA_ACCEPT_HEADER]: 'zstd' })
  })

  it('never returns a body that fails verification, even when the frame decodes', () => {
    const store = bound()
    const base = cards(300)

    decodeDeltaBody(BOARD, whole(base), base, store)

    const forged = delta(base, cards(301), { 'x-hermes-body-hash': bodyHash(cards(302)) })

    expect(() => decodeDeltaBody(BOARD, forged.response, forged.body, store)).toThrow(DeltaRetryError)
  })

  it('does not keep a whole body whose bytes are not the hashed bytes, a non-200, or an unbound backend', () => {
    const store = bound()
    const body = cards(50)

    // gzip on the wire: the hash is of the plain body, which we do not hold here.
    const zipped = gzipSync(body)

    expect(decodeDeltaBody(BOARD, whole(body, { 'content-encoding': 'gzip' }), zipped, store)).toBe(body.toString('utf8'))
    // A wrong hash header.
    decodeDeltaBody(BOARD, { headers: { 'x-hermes-body-hash': '0'.repeat(32) }, statusCode: 200 }, body, store)
    // An error answer.
    decodeDeltaBody(BOARD, { ...whole(body), statusCode: 500 }, body, store)
    // A local backend.
    decodeDeltaBody('http://127.0.0.1:9119/api/plugins/kanban/board', whole(body), body, store)

    expect(store.size).toBe(0)
  })

  it('passes an ordinary response through untouched: no headers, old backend', () => {
    const store = bound()
    const body = Buffer.from('{"ok":true}')

    expect(decodeDeltaBody(BOARD, { headers: {}, statusCode: 200 }, body, store)).toBe('{"ok":true}')
    expect(decodeDeltaBody(BOARD, { headers: { 'content-encoding': 'gzip' }, statusCode: 200 }, gzipSync(body), store)).toBe('{"ok":true}')
    expect(store.size).toBe(0)
  })
})

describe('the copy store', () => {
  it('is bounded by entries and by bytes, dropping the least recently used', () => {
    const store = bound()
    const body = Buffer.alloc(1024, 1)

    for (let i = 0; i < DELTA_STORE_MAX_ENTRIES + 10; i += 1) {
      store.put(`k${i}`, 'h', body)
    }

    expect(store.size).toBe(DELTA_STORE_MAX_ENTRIES)
    expect(store.get('k0')).toBeUndefined()
    expect(store.get(`k${DELTA_STORE_MAX_ENTRIES + 9}`)).toBeDefined()

    const big = Buffer.alloc(DELTA_STORE_MAX_BYTES / 2, 1)
    const bytes = new DeltaBodyStore()

    bytes.put('a', 'h', big)
    bytes.put('b', 'h', big)
    bytes.get('a') // a is now the most recently used
    bytes.put('c', 'h', big)

    expect(bytes.get('b')).toBeUndefined()
    expect(bytes.get('a')).toBeDefined()
    expect(bytes.bytes).toBeLessThanOrEqual(DELTA_STORE_MAX_BYTES)

    bytes.put('huge', 'h', Buffer.alloc(DELTA_STORE_MAX_BYTES + 1))
    expect(bytes.get('huge')).toBeUndefined()

    // One read keeps two copies at most, and sheds its own previous copy before evicting itself.
    const pair = new DeltaBodyStore()

    pair.put('r', 'h1', big)
    pair.put('r', 'h2', big)
    pair.put('r', 'h3', big)
    expect(pair.find('r', 'h1')).toBeUndefined()
    expect(pair.find('r', 'h2')).toBeDefined()
    expect(pair.get('r')?.hash).toBe('h3')
    expect(pair.bytes).toBe(DELTA_STORE_MAX_BYTES)

    pair.put('r', 'h4', Buffer.alloc(DELTA_STORE_MAX_BYTES / 2 + 1, 1))
    expect(pair.get('r')?.hash).toBe('h4')
    expect(pair.find('r', 'h3')).toBeUndefined()
    expect(pair.bytes).toBe(DELTA_STORE_MAX_BYTES / 2 + 1)
  })
})

describe('fetchJson itself (json-fetch.ts), against a live server that speaks the gateway protocol', () => {
  const seen: { base: string; accept: string; token: string }[] = []
  const known = new Map<string, Buffer>()
  let version = 0
  let damageNext = false

  const board = () => cards(3000, version)

  const server = http.createServer((req, res) => {
    const base = String(req.headers['x-hermes-delta-base'] ?? '')

    seen.push({ accept: String(req.headers['x-hermes-delta-accept'] ?? ''), base, token: String(req.headers['x-hermes-session-token'] ?? '') })

    const body = board()

    known.set(bodyHash(body), body)

    const held = known.get(base)

    if (!held) {
      res.writeHead(200, { 'Content-Type': 'application/json', 'X-Hermes-Body-Hash': bodyHash(body) })
      res.end(body)

      return
    }

    const frame = damageNext ? Buffer.from('damaged in transit') : zstdCompressSync(body, { dictionary: held })

    damageNext = false
    res.writeHead(200, {
      'Content-Type': 'application/vnd.hermes.delta+zstd',
      'X-Hermes-Body-Hash': bodyHash(body),
      'X-Hermes-Delta': 'zstd',
      'X-Hermes-Delta-Base': base
    })
    res.end(frame)
  })

  const deps: JsonFetchDeps = { nativeHeaders: async () => null, remoteHeaders: () => ({}) }

  afterAll(async () => {
    destroyKeepaliveAgents()
    server.closeAllConnections()
    await new Promise<void>(resolve => server.close(() => resolve()))
  })

  it('asks, takes the whole body once, then deltas; a damaged delta is retried for the whole body', async () => {
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))

    const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`
    const url = `${origin}/api/plugins/kanban/board?board=default`
    const read = () => fetchJsonWith(deps, url, 'session-token', {})

    // Not bound yet: an ordinary backend is not asked, and its answer is parsed as before.
    version = 1
    expect(await read()).toEqual(JSON.parse(board().toString('utf8')))
    expect(seen.at(-1)).toEqual({ accept: '', base: '', token: 'session-token' })

    noteRemoteConnect(origin, 'conn:fetch-json-test::default', Date.now(), () => undefined)

    expect(await read()).toEqual(JSON.parse(board().toString('utf8')))
    expect(seen.at(-1)).toMatchObject({ accept: 'zstd', base: '' })

    version = 2
    const second = bodyHash(cards(3000, 1))

    expect(await read()).toEqual(JSON.parse(board().toString('utf8')))
    expect(seen.at(-1)).toMatchObject({ accept: 'zstd', base: second })

    // The frame arrives damaged: the same call still resolves with the current body, after one retry with no base.
    version = 3
    damageNext = true

    const before = seen.length

    expect(await read()).toEqual(JSON.parse(board().toString('utf8')))
    expect(seen.slice(before)).toEqual([
      { accept: 'zstd', base: bodyHash(cards(3000, 2)), token: 'session-token' },
      { accept: 'zstd', base: '', token: 'session-token' }
    ])

    // And the next read is a delta again, against the body the retry delivered.
    version = 4
    expect(await read()).toEqual(JSON.parse(board().toString('utf8')))
    expect(seen.at(-1)).toMatchObject({ base: bodyHash(cards(3000, 3)) })
  })
})
