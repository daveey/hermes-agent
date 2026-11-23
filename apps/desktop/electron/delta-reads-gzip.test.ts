import http from 'node:http'
import type { AddressInfo } from 'node:net'
import { gzipSync, zstdCompressSync } from 'node:zlib'

import { afterEach, describe, expect, it } from 'vitest'

import { destroyKeepaliveAgents, withRetry } from './api-transport'
import { bodyHash, decodeDeltaBody, deltaBodyStore, DeltaBodyStore, DeltaRetryError, readEncodingHeaders } from './delta-reads'
import { GzipExpansionError } from './display-transcript'
import { fetchJsonWith } from './json-fetch'

const HUB = 'http://127.0.0.1:49921'
const URL = `${HUB}/api/sessions/example/messages?include_compacted=true&limit=200&order=latest&profile=default`
const ASK = 'X-Hermes-Delta-Accept'
const BASE = 'X-Hermes-Delta-Base'
const json = (version = 0) => JSON.stringify({ messages: [{ role: 'assistant', content: `héllo ${version}`.repeat(5000) }] })
const entity = (version = 0) => gzipSync(json(version))

const whole = (body: Buffer, headers: Record<string, string> = {}) => ({
  statusCode: 200,
  headers: { 'content-encoding': 'gzip', 'content-type': 'application/json', 'x-hermes-body-hash': bodyHash(body), ...headers }
})

const delta = (base: Buffer, next: Buffer, headers: Record<string, string> = {}) => ({
  body: zstdCompressSync(next, { dictionary: base }),
  response: { statusCode: 200, headers: {
    'x-hermes-delta': 'zstd', 'x-hermes-delta-base': bodyHash(base), 'x-hermes-body-hash': bodyHash(next),
    'x-hermes-delta-content-encoding': 'gzip', ...headers
  } }
})

function bound() {
  const store = new DeltaBodyStore()
  store.bindRemote(HUB, 'gzip-test')

  return store
}

afterEach(() => destroyKeepaliveAgents())

describe('negotiated gzip transcript deltas', () => {
  it('combines slim+gzip with the new capability only for remote transcript GETs', () => {
    const store = bound()
    expect(readEncodingHeaders('GET', URL, store)).toEqual({
      'Accept-Encoding': 'gzip', 'X-Hermes-Display-Transcript': '1', [ASK]: 'zstd-gzip-v1'
    })
    expect(readEncodingHeaders(undefined, URL, store)[ASK]).toBe('zstd-gzip-v1')
    expect(readEncodingHeaders('POST', URL, store)).toEqual({})
    expect(readEncodingHeaders('GET', URL, new DeltaBodyStore())).toEqual({
      'Accept-Encoding': 'gzip', 'X-Hermes-Display-Transcript': '1'
    })
    expect(readEncodingHeaders('GET', `${HUB}/api/sessions`, store)).toEqual({ [ASK]: 'zstd' })
  })

  it('whole gzip seeds exact compressed bytes, then unchanged and changed deltas yield exact JSON', () => {
    const store = bound()
    let base = entity()
    expect(decodeDeltaBody(URL, whole(base), base, store)).toBe(json())
    expect(store.get(store.keyFor(URL)!)!.body).toEqual(base)

    for (const version of [0, 1]) {
      const next = entity(version)
      expect(readEncodingHeaders('GET', URL, store)[BASE]).toBe(bodyHash(base))
      const encoded = delta(base, next)
      expect(decodeDeltaBody(URL, encoded.response, encoded.body, store)).toBe(json(version))
      expect(store.get(store.keyFor(URL)!)!.body).toEqual(next)
      base = next
    }
  })

  it('decodes a gzip-entity frame produced by Python compression.zstd with a prefix dictionary', () => {
    const store = bound()
    // Synthetic UTF-8 messages, gzip.compress(mtime=0), then web_delta_response.encode_delta.
    const base = Buffer.from('H4sIAAAAAAAC/5XUPQ7CMAxA4asgzx2wzW+vgjpEKAKk0Eg4W9UDcQ4uRo/wsnl4kz/Zi7xzRHrkkPG2yKeWLKOkiFe0NDcZ5F7nlrdplOfvW0rd7WUdWKm4NFw6Lg+4POLyhMszLi+4vPLNdyBxJeVMyp2UQymXUk6l3Eo5lnIt41rWcVNcy7iWcS3jWsa1jGsZ1zKu5VzLuZZ3vECu5VzLuZZzLedazrV805rWP8w2XRS8BgAA', 'base64')
    const frame = Buffer.from('KLUv/SC/fQUA9AkfiwgAAv+V1E0KwkAMQOGrlKy7MIm/cxVxUWRQYZyRTkFK6YE8hxfTI7zZZfFW+UgWecZah1usEs6LjCVFCTLU+qjTkCfp5VryFP9TkPv3k1LpNrL2rFRcGi4dl1tc7nC5x+UBl0dcnvjmG5C4knIm5U7KoZRLKadSbqUcS7mWcS1ruCmuZXIbXiAHWjm+uzG+0izrZf0BjVLgibwGAAAFACAAlhkIrsBnRLKACA==', 'base64')
    const messages = Array.from({ length: 40 }, (_, i) => ({ role: 'assistant', content: `héllo ${i}` }))
    decodeDeltaBody(URL, whole(base), base, store)
    messages[39].content = 'new reply'
    const response = delta(base, base).response
    response.headers['x-hermes-body-hash'] = 'dc61b3ffd9cb0c93756bd4c29149e43e'
    expect(decodeDeltaBody(URL, response, frame, store)).toBe(JSON.stringify({ messages }))
  })

  it('new client to old server decodes gzip without requiring a body hash', () => {
    const store = bound()
    expect(decodeDeltaBody(URL, { statusCode: 200, headers: { 'content-encoding': 'gzip' } }, entity(), store)).toBe(json())
    expect(store.size).toBe(0)
  })

  it('does not cache whole gzip outside negotiated transcript reads or on non-200', () => {
    const store = bound()
    const body = entity()
    decodeDeltaBody(`${HUB}/api/sessions`, whole(body), body, store)
    decodeDeltaBody(URL, { ...whole(body), statusCode: 404 }, body, store)
    expect(store.size).toBe(0)
  })

  it.each(['missing base', 'corrupt base', 'bad hash', 'missing hash', 'bad frame', 'unknown coding', 'outer coding', 'bad gzip'])('drops the base and retries for %s', async kind => {
    const store = bound()
    const base = entity()
    decodeDeltaBody(URL, whole(base), base, store)
    expect(store.get(store.keyFor(URL)!)?.body).toEqual(base)
    let next = entity(1)
    const headers: Record<string, string> = {}

    if (kind === 'missing base') { store.delete(store.keyFor(URL)!) }

    if (kind === 'corrupt base') { store.put(store.keyFor(URL)!, bodyHash(base), Buffer.from('damaged')) }

    if (kind === 'bad hash') { headers['x-hermes-body-hash'] = 'f'.repeat(32) }

    if (kind === 'missing hash') { headers['x-hermes-body-hash'] = '' }

    if (kind === 'unknown coding') { headers['x-hermes-delta-content-encoding'] = 'br' }

    if (kind === 'outer coding') { headers['content-encoding'] = 'gzip' }

    if (kind === 'bad gzip') { next = Buffer.from('not gzip') }
    const encoded = delta(base, next, headers)

    if (kind === 'bad frame') { encoded.body = Buffer.from('invalid frame') }
    expect(() => decodeDeltaBody(URL, encoded.response, encoded.body, store)).toThrow(DeltaRetryError)
    expect(readEncodingHeaders('GET', URL, store)).not.toHaveProperty(BASE)
    // Existing retry budget, no new retry loop: the second response is ordinary gzip.
    let attempts = 0

    const result = await withRetry(async () => {
      attempts += 1

      if (attempts === 1) { throw new DeltaRetryError(URL, 'injected') }

      return decodeDeltaBody(URL, whole(entity(1)), entity(1), store)
    }, { method: 'GET', retries: 1, baseDelayMs: 1 })

    expect(result).toBe(json(1))
    expect(attempts).toBe(2)
  })

  it('does not retain a whole entity with a corrupt hash', () => {
    const store = bound()
    const body = entity()
    expect(() => decodeDeltaBody(URL, whole(body, { 'x-hermes-body-hash': 'f'.repeat(32) }), body, store)).toThrow(DeltaRetryError)
    expect(store.size).toBe(0)
  })

  it('rejects gzip expansion past the decompressed budget, including within a verified delta', () => {
    const store = bound()
    const base = entity()
    decodeDeltaBody(URL, whole(base), base, store)
    const bomb = gzipSync(Buffer.alloc(16 * 1024 * 1024 + 1, 65))
    const encoded = delta(base, bomb)
    expect(() => decodeDeltaBody(URL, encoded.response, encoded.body, store)).toThrow(GzipExpansionError)
    expect(store.size).toBe(0)
  })

  it('rejects reconstructed compressed entities above the entity budget', () => {
    const store = bound()
    const base = entity()
    decodeDeltaBody(URL, whole(base), base, store)
    const encoded = delta(base, Buffer.alloc(8 * 1024 * 1024 + 1, 65))
    expect(() => decodeDeltaBody(URL, encoded.response, encoded.body, store)).toThrow('bad frame')
    expect(store.size).toBe(0)
  })

  it('refuses gzip deltas on ordinary list reads even when a base is held', () => {
    const store = bound()
    const url = `${HUB}/api/sessions`
    const base = entity()
    store.put(store.keyFor(url)!, bodyHash(base), base)
    const encoded = delta(base, entity(1))
    expect(() => decodeDeltaBody(url, encoded.response, encoded.body, store)).toThrow('unexpected outer or original')
    expect(store.size).toBe(0)
  })

  it.each(['local', 'old remote', 'whole', 'delta'])('recovers an oversized valid gzip page from %s with one identity retry', async mode => {
    const page = { messages: Array.from({ length: 200 }, () => ({ role: 'assistant', content: 'x'.repeat(90_000) })) }
    const text = JSON.stringify(page)
    const zipped = gzipSync(text)
    const base = entity()
    const requests: http.IncomingHttpHeaders[] = []

    const server = http.createServer((req, res) => {
      requests.push(req.headers)

      if (req.headers['accept-encoding'] === 'identity') {
        res.writeHead(200, { 'content-type': 'application/json' })
        res.end(text)
      } else if (mode === 'delta') {
        const frame = delta(base, zipped)
        res.writeHead(200, frame.response.headers)
        res.end(frame.body)
      } else {
        res.writeHead(200, mode === 'whole' ? whole(zipped).headers : { 'content-encoding': 'gzip' })
        res.end(zipped)
      }
    })

    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`
    const url = `${origin}/api/sessions/example/messages?limit=200&order=latest`
    const scope = `large-page-${mode}`

    if (mode !== 'local') { deltaBodyStore.bindRemote(origin, scope) }

    if (mode === 'delta') { deltaBodyStore.put(deltaBodyStore.keyFor(url)!, bodyHash(base), base) }

    try {
      expect(await fetchJsonWith({ remoteHeaders: () => ({}), nativeHeaders: async () => null }, url, 'test')).toEqual(page)
      expect(requests).toHaveLength(2)
      expect(requests[1]['accept-encoding']).toBe('identity')
      expect(requests[1]['x-hermes-display-transcript']).toBe('1')
      expect(requests[1]['x-hermes-delta-accept']).toBeUndefined()
      expect(requests[1]['x-hermes-delta-base']).toBeUndefined()

      if (mode !== 'local') { expect(deltaBodyStore.get(deltaBodyStore.keyFor(url)!)).toBeUndefined() }
    } finally {
      deltaBodyStore.unbindScope(scope)
      server.closeAllConnections()
      await new Promise<void>(resolve => server.close(() => resolve()))
    }
  })

  it.each(['oversized identity', 'ignores identity'])('bounds recovery when the server %s', async mode => {
    const zipped = gzipSync(Buffer.alloc(16 * 1024 * 1024 + 1, 65))
    let requests = 0

    const server = http.createServer((req, res) => {
      requests += 1

      if (req.headers['accept-encoding'] !== 'identity' || mode === 'ignores identity') {
        res.writeHead(200, { 'content-encoding': 'gzip' })
        res.end(zipped)
      } else {
        // Chunked: no Content-Length to trust, and no single huge fixture allocation.
        res.writeHead(200, { 'content-type': 'application/json' })
        const chunk = Buffer.alloc(1024 * 1024, 65)

        for (let i = 0; i < 65; i += 1) { res.write(chunk) }
        res.end()
      }
    })

    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/api/sessions/example/messages?limit=200`

    try {
      await expect(fetchJsonWith({ remoteHeaders: () => ({}), nativeHeaders: async () => null }, url, 'test'))
        .rejects.toThrow(mode === 'oversized identity' ? '64 MiB recovery budget' : 'Gzip transcript exceeds')
      expect(requests).toBe(2)
    } finally {
      server.closeAllConnections()
      await new Promise<void>(resolve => server.close(() => resolve()))
    }
  })

  it('fetchJson negotiates over real HTTP, decodes gzip/deltas, and retries a bad delta without a base', async () => {
    const requests: http.IncomingHttpHeaders[] = []
    const v0 = entity()
    const v1 = entity(1)

    const server = http.createServer((req, res) => {
      requests.push(req.headers)

      if (requests.length === 2 || requests.length === 3) {
        const frame = delta(v0, requests.length === 2 ? v0 : v1)

        if (requests.length === 3) { frame.response.headers['x-hermes-body-hash'] = 'f'.repeat(32) }
        res.writeHead(200, frame.response.headers)
        res.end(frame.body)
      } else {
        const body = requests.length === 1 ? v0 : v1
        res.writeHead(200, whole(body).headers)
        res.end(body)
      }
    })

    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`
    const url = `${origin}/api/sessions/example/messages?limit=200&order=latest`
    const scope = 'gzip-http-test'
    deltaBodyStore.bindRemote(origin, scope)
    const deps = { remoteHeaders: () => ({}), nativeHeaders: async () => null }

    try {
      expect(await fetchJsonWith(deps, url, 'test')).toEqual(JSON.parse(json()))
      expect(await fetchJsonWith(deps, url, 'test')).toEqual(JSON.parse(json()))
      expect(await fetchJsonWith(deps, url, 'test')).toEqual(JSON.parse(json(1)))
      expect(requests).toHaveLength(4)

      for (const req of requests) {
        expect(req['x-hermes-delta-accept']).toBe('zstd-gzip-v1')
        expect(req['x-hermes-display-transcript']).toBe('1')
        expect(req['accept-encoding']).toBe('gzip')
      }

      expect(requests.map(req => req['x-hermes-delta-base'])).toEqual([undefined, bodyHash(v0), bodyHash(v0), undefined])
    } finally {
      deltaBodyStore.delete(deltaBodyStore.keyFor(url)!)
      deltaBodyStore.unbindScope(scope)
      server.closeAllConnections()
      await new Promise<void>(resolve => server.close(() => resolve()))
    }
  })
})
