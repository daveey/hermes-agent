// [daveey] Delta responses: a large read of a remote backend arrives as the difference from the copy we already hold.
//
// Measured 2026-11-13 against the hub: six list/poll reads (sessions sidebar, projects tree, kanban board, sessions
// list, cron jobs, runners fleet) carry almost all the bytes the Desktop pulls through its SSH tunnel, 2,052 KB for
// one round. Fetched again 20 s later and encoded against the first copy they are 12 KB (gzip: 286 KB), for under
// 8 ms of CPU each. On a 60-175 KB/s link that is the difference between the polls filling the link and not.
//
// Protocol (gateway half: hermes_cli/web_delta_response.py). Nothing here changes WHAT a request returns or when:
// every request still reaches the backend and gets its current answer. Only the encoding on the wire changes.
// - A GET to an SSH-tunnelled backend says `X-Hermes-Delta-Accept: zstd` and, when a copy of that read is held,
//   `X-Hermes-Delta-Base: <hash of the copy>`.
// - A large JSON 200 comes back either whole with `X-Hermes-Body-Hash` (we keep it as the next base), or as
//   `X-Hermes-Delta: zstd` + `X-Hermes-Delta-Base` + `X-Hermes-Body-Hash`: a zstd frame that decodes against the
//   base. The decoded body must hash to X-Hermes-Body-Hash; anything else (base gone, wrong base, bad frame, wrong
//   hash) drops the copy and fails the attempt with DELTA_RETRY_CODE, which api-transport retries, now without a
//   base, so the backend sends the whole body.
// A backend that predates this ignores the request headers and answers as before.
//
// Copies are kept per remote scope and path (not per tunnel port), so the full refetch after a tunnel rebuild,
// which is when a slow link hurts most, is a delta too. Memory only: a relaunch starts with whole bodies.

import { createHash } from 'node:crypto'
import { zstdDecompressSync } from 'node:zlib'

import { decodeResponseBody, displayTranscriptHeaders } from './display-transcript'

export const DELTA_ACCEPT_HEADER = 'X-Hermes-Delta-Accept'
export const DELTA_BASE_HEADER = 'X-Hermes-Delta-Base'
export const DELTA_ENCODING = 'zstd'
export const DELTA_RETRY_CODE = 'HERMES_DELTA_RETRY'
export const DELTA_STORE_MAX_BYTES = 48 * 1024 * 1024
export const DELTA_STORE_MAX_ENTRIES = 96
export const DELTA_COPIES_PER_READ = 2
// A decoded body larger than this is never a delta we asked for (the gateway caps what it encodes).
export const DELTA_MAX_DECODED_BYTES = 16 * 1024 * 1024

export const bodyHash = (body: Buffer) => createHash('sha256').update(body).digest('hex').slice(0, 32)

type HeaderValue = string | string[] | undefined

const header = (headers: Record<string, HeaderValue>, name: string) => {
  const value = headers[name.toLowerCase()]

  return String(Array.isArray(value) ? value[0] : (value ?? '')).trim()
}

export class DeltaRetryError extends Error {
  readonly code = DELTA_RETRY_CODE

  constructor(url: string, why: string) {
    super(`Delta response for ${url} could not be used (${why}); retrying for the whole body`)
    this.name = 'DeltaRetryError'
  }
}

interface Copy {
  body: Buffer
  hash: string
}

/**
 * The copies deltas are decoded against, and which tunnel baseUrl belongs to which remote scope.
 *
 * Each read keeps its newest copy and the one before it. Two reads of the same URL can be in flight at once: both
 * name the same base, the first answer replaces it, and the second answer's delta still names the older one. With
 * only the newest copy that second read would fail and refetch the whole body.
 */
export class DeltaBodyStore {
  // Newest copy last, at most COPIES_PER_READ per key; keys in least-recently-used order.
  readonly #copies = new Map<string, Copy[]>()
  readonly #scopes = new Map<string, string>()
  #bytes = 0

  /** A remote (SSH-tunnelled) backend now answers at baseUrl for this pool scope. */
  bindRemote(baseUrl: null | string | undefined, scope: string): void {
    const origin = originOf(baseUrl)

    if (origin && scope) {
      this.#scopes.set(origin, scope)
    }
  }

  /**
   * The scope's tunnel is gone: its local port may be handed to something else, which must not be asked for
   * deltas or have its answers filed under this scope. The copies stay: a rebuilt tunnel binds a new port.
   */
  unbindScope(scope: string): void {
    for (const [origin, bound] of this.#scopes) {
      if (bound === scope) {
        this.#scopes.delete(origin)
      }
    }
  }

  /** `<scope><path+query>` for a URL of a bound remote backend; null for anything else (local backends included). */
  keyFor(url: string): null | string {
    try {
      const parsed = new URL(url)
      const scope = this.#scopes.get(parsed.origin)

      return scope ? `${scope}\u0000${parsed.pathname}${parsed.search}` : null
    } catch {
      return null
    }
  }

  /** The newest copy of a read: the base to name in the next request. */
  get(key: string): Copy | undefined {
    return this.#touch(key)?.at(-1)
  }

  /** The held copy with this hash, newest or previous: the base a delta response says it was encoded against. */
  find(key: string, hash: string): Copy | undefined {
    return this.#touch(key)?.find(copy => copy.hash === hash)
  }

  put(key: string, hash: string, body: Buffer): void {
    if (body.length > DELTA_STORE_MAX_BYTES) {
      this.delete(key)

      return
    }

    const kept = (this.#copies.get(key) ?? []).filter(copy => copy.hash !== hash)

    this.delete(key)

    const copies = [...kept, { body, hash }].slice(-DELTA_COPIES_PER_READ)

    this.#copies.set(key, copies)
    this.#bytes += copies.reduce((sum, copy) => sum + copy.body.length, 0)

    while (this.#bytes > DELTA_STORE_MAX_BYTES || this.#copies.size > DELTA_STORE_MAX_ENTRIES) {
      const oldest = this.#copies.keys().next().value as string
      const [previous, ...rest] = this.#copies.get(oldest) ?? []

      // Shed a read's previous copy before the read itself, unless it is another read's turn to go entirely.
      if (oldest === key && rest.length > 0 && previous) {
        this.#copies.set(key, rest)
        this.#bytes -= previous.body.length
      } else {
        this.delete(oldest)
      }
    }
  }

  delete(key: string): void {
    for (const copy of this.#copies.get(key) ?? []) {
      this.#bytes -= copy.body.length
    }

    this.#copies.delete(key)
  }

  get size(): number {
    return this.#copies.size
  }

  get bytes(): number {
    return this.#bytes
  }

  #touch(key: string): Copy[] | undefined {
    const copies = this.#copies.get(key)

    if (copies) {
      this.#copies.delete(key) // most recently used last
      this.#copies.set(key, copies)
    }

    return copies
  }
}

function originOf(baseUrl: null | string | undefined): string {
  try {
    return new URL(String(baseUrl ?? '')).origin
  } catch {
    return ''
  }
}

export const deltaBodyStore = new DeltaBodyStore()

/** Request headers that ask a remote backend for a delta. Empty for a non-GET and for a backend that is not bound. */
export function deltaRequestHeaders(
  method: string | undefined,
  url: string,
  store: DeltaBodyStore = deltaBodyStore
): Record<string, string> {
  if (String(method || 'GET').toUpperCase() !== 'GET') {
    return {}
  }

  const key = store.keyFor(url)

  if (!key) {
    return {}
  }

  const copy = store.get(key)

  return { [DELTA_ACCEPT_HEADER]: DELTA_ENCODING, ...(copy ? { [DELTA_BASE_HEADER]: copy.hash } : {}) }
}

/**
 * Every "how to encode the answer" request header fetchJson sends: the transcript slim/gzip ask and the delta ask.
 * A transcript page read asks for gzip, which the gateway never delta-encodes, so the two do not combine.
 */
export function readEncodingHeaders(
  method: string | undefined,
  url: string,
  store: DeltaBodyStore = deltaBodyStore
): Record<string, string> {
  const transcript = displayTranscriptHeaders(method, url)

  return Object.keys(transcript).length > 0 ? transcript : deltaRequestHeaders(method, url, store)
}

/**
 * The response body as text. A delta is decoded against the held copy and verified; a whole body that carries a
 * hash becomes the next base. Everything else passes through decodeResponseBody unchanged.
 */
export function decodeDeltaBody(
  url: string,
  response: { headers: Record<string, HeaderValue>; statusCode?: number },
  body: Buffer,
  store: DeltaBodyStore = deltaBodyStore
): string {
  const { headers } = response
  const hash = header(headers, 'x-hermes-body-hash')
  const key = store.keyFor(url)

  if (header(headers, 'x-hermes-delta') !== DELTA_ENCODING) {
    // Whole body. Keep it as the next base only when the bytes we hold are exactly the bytes that were hashed.
    if (key && hash && response.statusCode === 200 && !header(headers, 'content-encoding') && bodyHash(body) === hash) {
      store.put(key, hash, body)
    }

    return decodeResponseBody(body, headers['content-encoding'])
  }

  // Every failure drops this read's copies, so the retry names no base and cannot fail the same way again.
  const fail = (why: string): never => {
    if (key) {
      store.delete(key)
    }

    throw new DeltaRetryError(url, why)
  }

  if (!key || !hash || response.statusCode !== 200) {
    return fail('not a 200 with a hash from a bound remote backend')
  }

  const base = store.find(key, header(headers, 'x-hermes-delta-base'))

  if (!base) {
    return fail('the base it was encoded against is not held')
  }

  let decoded: Buffer

  try {
    decoded = zstdDecompressSync(body, { dictionary: base.body, maxOutputLength: DELTA_MAX_DECODED_BYTES })
  } catch (error) {
    return fail(`bad frame: ${String((error as Error)?.message ?? error).slice(0, 80)}`)
  }

  if (bodyHash(decoded) !== hash) {
    return fail('decoded body does not match its hash')
  }

  store.put(key, hash, decoded)

  return decoded.toString('utf8')
}
