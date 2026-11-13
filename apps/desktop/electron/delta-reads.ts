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
// which is when a slow link hurts most, is a delta too. With persistTo() (main.ts: <userData>/delta-bodies) each
// read's newest copy also outlives a relaunch: written a moment after it arrives (never on the request path), one
// 0600 file per read, the same byte budget, least recently written out; read back lazily on the first use of that
// read. A file is a base only when its body hashes to the hash it records and it names the same scope and path;
// anything else is removed and that read goes whole, as it would have anyway. A copy only ever names a base: every
// read still reaches the backend.

import { createHash, randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
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
// How long a new copy waits before it is written to disk: a burst of reads costs one write per read.
export const DELTA_DISK_FLUSH_MS = 2_000
// An unchanged copy is not written again; its file is only marked recently used, at most this often.
export const DELTA_DISK_TOUCH_MS = 60_000
const DISK_FORMAT = 'hermes-delta-base/1'
const DISK_SUFFIX = '.base'

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

interface Disk {
  dir: string
  dirty: Set<string>
  deleted: Set<string>
  // What each read's file holds (the hash last written or loaded) and when it was last written or touched.
  saved: Map<string, { at: number; hash: string }>
  tried: Set<string>
  timer: null | ReturnType<typeof setTimeout>
}

const diskName = (key: string) => `${createHash('sha256').update(key).digest('hex').slice(0, 32)}${DISK_SUFFIX}`

/** One persisted copy: a JSON header line naming the read and the body's hash, then the body's exact bytes. */
function encodeDiskCopy(key: string, copy: Copy): Buffer {
  return Buffer.concat([Buffer.from(`${JSON.stringify({ format: DISK_FORMAT, hash: copy.hash, key })}\n`), copy.body])
}

function decodeDiskCopy(key: string, data: Buffer): Copy | null {
  const eol = data.indexOf(0x0a)

  if (eol < 0) {
    return null
  }

  try {
    const meta = JSON.parse(data.subarray(0, eol).toString('utf8')) as {
      format?: unknown
      hash?: unknown
      key?: unknown
    }

    const body = Buffer.from(data.subarray(eol + 1))

    if (
      meta.format !== DISK_FORMAT ||
      meta.key !== key ||
      typeof meta.hash !== 'string' ||
      bodyHash(body) !== meta.hash
    ) {
      return null
    }

    return { body, hash: meta.hash }
  } catch {
    return null
  }
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
  #disk: Disk | null = null
  #flushing: Promise<void> | null = null
  #flushEpoch = 0

  /**
   * Keep each read's newest copy in `dir` as well, so a relaunch still holds the bases (module comment). Nothing is
   * read until a read asks for its copy; nothing is written on the request path.
   */
  persistTo(dir: string): void {
    this.#disk = { dir, dirty: new Set(), deleted: new Set(), saved: new Map(), tried: new Set(), timer: null }
  }

  /**
   * A remote (SSH-tunnelled) backend now answers at baseUrl for this pool scope. A scope has one tunnel, so one
   * origin: a rebuild on a new local port takes the scope (and its copies) with it, and the old port, which may be
   * handed to something else, is no longer asked for deltas or able to file answers under this scope.
   */
  bindRemote(baseUrl: null | string | undefined, scope: string): void {
    const origin = originOf(baseUrl)

    if (origin && scope) {
      this.unbindScope(scope)
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
    return (this.#touch(key) ?? this.#load(key))?.at(-1)
  }

  /** The held copy with this hash, newest or previous: the base a delta response says it was encoded against. */
  find(key: string, hash: string): Copy | undefined {
    return (this.#touch(key) ?? this.#load(key))?.find(copy => copy.hash === hash)
  }

  put(key: string, hash: string, body: Buffer): void {
    if (body.length > DELTA_STORE_MAX_BYTES) {
      this.delete(key)

      return
    }

    this.#disk?.deleted.delete(key)
    this.#hold(key, hash, body)

    if (!this.#alreadyOnDisk(key, hash)) {
      this.#markDirty(key)
    }
  }

  /**
   * The read's file already holds this copy (an unchanged poll, or the copy just loaded from it): nothing to write.
   * Its file is marked recently used now and then, so the disk budget drops it last; if the file is gone (trimmed,
   * removed), the next copy is written again.
   */
  #alreadyOnDisk(key: string, hash: string): boolean {
    const disk = this.#disk
    const saved = disk?.saved.get(key)

    if (!disk || !saved || saved.hash !== hash || disk.dirty.has(key)) {
      return false
    }

    const now = Date.now()

    if (now - saved.at >= DELTA_DISK_TOUCH_MS) {
      const at = new Date(now)

      saved.at = now
      fs.utimes(path.join(disk.dir, diskName(key)), at, at, error => {
        if (error && disk.saved.get(key) === saved) {
          disk.saved.delete(key)
        }
      })
    }

    return true
  }

  #hold(key: string, hash: string, body: Buffer): void {
    const kept = (this.#copies.get(key) ?? []).filter(copy => copy.hash !== hash)

    this.#drop(key)

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
        this.#drop(oldest) // memory only: the disk keeps its own budget
      }
    }
  }

  /** Forget a read, on disk too: its copy failed (or is too large to keep), so no relaunch may name it again. */
  delete(key: string): void {
    this.#drop(key)

    const disk = this.#disk

    if (disk) {
      disk.saved.delete(key)
      disk.deleted.add(key)
      disk.tried.add(key)
      this.#markDirty(key)
      void this.flush() // removal shares the write publication fence, off the request path
    }
  }

  #drop(key: string): void {
    for (const copy of this.#copies.get(key) ?? []) {
      this.#bytes -= copy.body.length
    }

    this.#copies.delete(key)
  }

  /** Write changed copies off the request path. Dirty ownership lasts until publication, including during quit. */
  async flush(): Promise<void> {
    while (this.#flushing) {await this.#flushing}

    const pending = this.#flushPending()

    this.#flushing = pending

    try {
      await pending
    } finally {
      if (this.#flushing === pending) {this.#flushing = null}
    }
  }

  async #flushPending(): Promise<void> {
    const disk = this.#disk

    if (!disk) {return}

    if (disk.timer) {
      clearTimeout(disk.timer)
      disk.timer = null
    }

    const keys = [...disk.dirty]
    const epoch = this.#flushEpoch

    try {
      await fs.promises.mkdir(disk.dir, { mode: 0o700, recursive: true })

      for (const key of keys) {
        if (epoch !== this.#flushEpoch || disk !== this.#disk) {return}

        const copy = this.#copies.get(key)?.at(-1)
        const target = path.join(disk.dir, diskName(key))

        if (copy) {
          const tmp = `${target}.${randomUUID()}.tmp`

          try {
            await fs.promises.writeFile(tmp, encodeDiskCopy(key, copy), { mode: 0o600 })

            // No await between validation and rename: delete/put/quit cannot interleave with publication.
            if (epoch !== this.#flushEpoch || disk !== this.#disk || this.#copies.get(key)?.at(-1) !== copy) {continue}
            fs.renameSync(tmp, target)
            disk.saved.set(key, { at: Date.now(), hash: copy.hash })
          } finally {
            fs.rmSync(tmp, { force: true })
          }
        } else if (disk.deleted.has(key)) {
          fs.rmSync(target, { force: true })
        }

        disk.dirty.delete(key)
        disk.deleted.delete(key)
      }
    } catch {
      // Persistence is an optimisation: an unwritable directory leaves the copies in memory and dirty for retry.
    } finally {
      if (epoch === this.#flushEpoch) {this.#trimDisk(disk.dir)}
    }
  }

  /** The same flush, synchronously, for quit (an async write would not finish before the process exits). */
  flushSync(): void {
    const disk = this.#disk

    if (!disk) {
      return
    }

    if (disk.timer) {
      clearTimeout(disk.timer)
      disk.timer = null
    }

    const keys = [...disk.dirty]

    this.#flushEpoch += 1 // quit supersedes every captured async copy

    try {
      fs.mkdirSync(disk.dir, { mode: 0o700, recursive: true })

      for (const key of keys) {
        const copy = this.#copies.get(key)?.at(-1)

        if (copy) {
          const target = path.join(disk.dir, diskName(key))
          const tmp = `${target}.${randomUUID()}.tmp`

          try {
            fs.writeFileSync(tmp, encodeDiskCopy(key, copy), { mode: 0o600 })
            fs.renameSync(tmp, target)
            disk.saved.set(key, { at: Date.now(), hash: copy.hash })
          } finally {
            fs.rmSync(tmp, { force: true })
          }
        } else if (disk.deleted.has(key)) {
          fs.rmSync(path.join(disk.dir, diskName(key)), { force: true })
        }

        disk.dirty.delete(key)
        disk.deleted.delete(key)
      }
    } catch {
      // as flush()
    } finally {
      this.#trimDisk(disk.dir)
    }
  }

  #markDirty(key: string): void {
    const disk = this.#disk

    if (!disk) {
      return
    }

    disk.dirty.add(key)
    disk.tried.add(key) // memory now holds a newer copy than any file

    if (!disk.timer) {
      disk.timer = setTimeout(() => void this.flush(), DELTA_DISK_FLUSH_MS)
      disk.timer.unref?.()
    }
  }

  #trimDisk(dir: string): void {
    try {
      this.#trimDiskFiles(dir)
    } catch {
      // Missing/unwritable cache: persistence must not interfere with reads or quitting.
    }
  }

  #trimDiskFiles(dir: string): void {
    const files: { at: number; file: string; size: number }[] = []

    for (const name of fs.readdirSync(dir)) {
      // Interrupted writes count too; otherwise repeated hard exits could grow unbounded temporary files.
      if (name.endsWith(DISK_SUFFIX) || /^[a-f0-9]{32}\.base\.[\da-f-]+\.tmp$/.test(name)) {
        try {
          const stat = fs.statSync(path.join(dir, name))

          files.push({ at: stat.mtimeMs, file: path.join(dir, name), size: stat.size })
        } catch {
          // gone already
        }
      }
    }

    let total = files.reduce((sum, file) => sum + file.size, 0)

    for (const file of files.sort((a, b) => a.at - b.at)) {
      if (total <= DELTA_STORE_MAX_BYTES) {
        break
      }

      fs.rmSync(file.file, { force: true })
      total -= file.size
    }
  }

  /** A read's persisted copy, held from now on; once per read and process, and only for a read memory lacks. */
  #load(key: string): Copy[] | undefined {
    const disk = this.#disk

    if (!disk || disk.tried.has(key)) {
      return undefined
    }

    disk.tried.add(key)

    const file = path.join(disk.dir, diskName(key))
    let data: Buffer

    try {
      data = fs.readFileSync(file)
    } catch {
      return undefined
    }

    const copy = data.length <= DELTA_STORE_MAX_BYTES + 4096 ? decodeDiskCopy(key, data) : null

    if (!copy) {
      this.delete(key) // truncated, damaged or another read's: never a base

      return undefined
    }

    const now = new Date()

    fs.utimes(file, now, now, () => undefined) // recently used: last out of the disk budget
    disk.saved.set(key, { at: now.getTime(), hash: copy.hash })
    this.#hold(key, copy.hash, copy.body)

    return this.#copies.get(key)
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

/** main.ts: the shared store keeps its copies under userData, and writes what is still pending as the app quits. */
export function persistDeltaBases(
  app: { getPath(name: 'userData'): string; on(event: 'will-quit', listener: () => void): unknown },
  store: DeltaBodyStore = deltaBodyStore
): void {
  store.persistTo(path.join(app.getPath('userData'), 'delta-bodies'))
  app.on('will-quit', () => store.flushSync())
}

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
