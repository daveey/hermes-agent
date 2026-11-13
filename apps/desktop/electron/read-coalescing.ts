// [daveey] One wire request for identical concurrent reads of a backend.
//
// Measured 2026-11-13 on the hub's loopback port (30 s pcap of the Desktop's SSH tunnel): the same session-list
// GET left this Desktop up to 6 times within 1.5 s (sidebar slices, cron list, per-profile merges and windows each
// asking for profile=all), each answered with the same 120-273 KB body. Repeats of a read still in flight were 44%
// of every byte the hub sent (4.1 of 9.5 MB in 101 requests); on a lossy remote link those bursts queued the other
// requests past their deadline ("Timed out connecting to Hermes backend", 72 on conn=hermes-hub in 30 min).
//
// A GET that is identical to one still in flight (same backend, credential, headers, path, deadline, pool) now
// waits for that one's answer instead of sending its own. Each caller gets its own copy of the answer, so a caller
// that edits its result cannot change another's. A write to a backend starts a new generation: a read issued after
// it never joins a read that left before it, so read-after-write still sees the write.

export interface CoalescibleRead {
  method?: string
  body?: unknown
  upload?: unknown
}

const copyOf = <T>(value: T): T => {
  try {
    return structuredClone(value)
  } catch {
    return value // not cloneable (never true for parsed JSON): hand out the shared value rather than fail the read
  }
}

export class ReadCoalescer {
  readonly #inflight = new Map<string, { joined: number; promise: Promise<unknown> }>()
  readonly #generations = new Map<string, number>()

  /** True for a plain read: a GET (or no method) with no body and no upload. */
  static isRead(opts: CoalescibleRead = {}): boolean {
    return String(opts.method || 'GET').toUpperCase() === 'GET' && opts.body === undefined && !opts.upload
  }

  /** A write to `origin` is starting: reads issued from now on never join one that left before it. */
  noteWrite(origin: string): void {
    this.#generations.set(origin, (this.#generations.get(origin) ?? 0) + 1)
  }

  /** The key identical reads share; `parts` must already name the credential, headers, path and deadline. */
  keyFor(origin: string, parts: unknown[]): string {
    return JSON.stringify([origin, this.#generations.get(origin) ?? 0, ...parts])
  }

  /** Run `start` unless a read with this key is in flight; then share its answer (each caller gets a copy). */
  run<T>(key: null | string, start: () => Promise<T>): Promise<T> {
    if (!key) {
      return start()
    }

    const existing = this.#inflight.get(key)

    if (existing) {
      existing.joined += 1

      return existing.promise.then(value => copyOf(value as T))
    }

    const entry = { joined: 0, promise: Promise.resolve() as Promise<unknown> }

    // Settled reads leave the map before any caller sees the answer, so a later read always asks again.
    entry.promise = Promise.resolve()
      .then(start)
      .finally(() => {
        if (this.#inflight.get(key) === entry) {
          this.#inflight.delete(key)
        }
      })
    this.#inflight.set(key, entry)

    // The first caller keeps the original only when nobody joined; otherwise everyone works on a copy.
    return entry.promise.then(value => (entry.joined > 0 ? copyOf(value as T) : (value as T)))
  }

  get inflight(): number {
    return this.#inflight.size
  }
}

export const backendReadCoalescer = new ReadCoalescer()

export interface BackendReadDescriptor {
  baseUrl?: null | string
  authMode?: null | string
  token?: null | string
  headers?: unknown
  gatewayEndpoint?: unknown
}

export interface BackendReadOpts extends CoalescibleRead {
  timeoutMs?: number
  // 'probe' = the dispatch health probe's dedicated socket pool (api-transport.ts).
  pool?: 'json' | 'probe'
  profileScoped?: boolean
}

/**
 * fetchJsonForBackend's front door (main.ts): a write goes straight out and starts a new generation for its
 * backend; a read shares an identical read still in flight. Everything that changes what goes on the wire, or what
 * the caller is willing to wait, is in the key, so only truly identical requests share one answer.
 */
export function coalescedBackendRead<D extends BackendReadDescriptor, O extends BackendReadOpts, T>(
  coalescer: ReadCoalescer,
  descriptor: D,
  path: string,
  opts: O,
  once: (descriptor: D, path: string, opts: O) => Promise<T>
): Promise<T> {
  const origin = String(descriptor?.baseUrl ?? '')

  if (!ReadCoalescer.isRead(opts)) {
    coalescer.noteWrite(origin)

    return once(descriptor, path, opts)
  }

  const key = coalescer.keyFor(origin, [
    descriptor?.authMode ?? '',
    descriptor?.token ?? '',
    descriptor?.headers ?? null,
    Boolean(descriptor?.gatewayEndpoint),
    path,
    opts.timeoutMs ?? null,
    opts.pool ?? '',
    Boolean(opts.profileScoped)
  ])

  return coalescer.run(key, () => once(descriptor, path, opts))
}
