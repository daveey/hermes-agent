/**
 * [daveey] Off-main-thread access to the transcript cache store.
 *
 * TranscriptCacheStore is synchronous SQLite. Run on Electron's main thread, one prefetch
 * write that waited on the disk (a WAL checkpoint fsync, cold pages under memory pressure
 * while a CI job saturated the SSD) froze the main loop for 1-1.8 s: measured on the mini,
 * the row's own `saved_at` put 1.6 s of a 1.76 s stall inside `write()`. Desktop therefore
 * runs the store on a worker thread and the main process only awaits it. Keep every store
 * call behind this client; never open the cache file on the main thread while a worker owns it.
 *
 * `inlineClient` is the same interface over an in-thread store: tests, and the fallback when
 * the worker cannot start (the cache then works as before, stalls included, and says so).
 */
import fs from 'node:fs'
import { Worker } from 'node:worker_threads'

import type { TranscriptCacheStore } from './transcript-cache'

export const STORE_OPS = [
  'clear',
  'drop',
  'dropEverywhere',
  'getMeta',
  'meta',
  'metaMany',
  'migrateProfile',
  'putLatest',
  'putOlder',
  'read',
  'setMeta',
  'stats'
] as const

export type StoreOp = (typeof STORE_OPS)[number]

export interface StoreRequest {
  args: unknown[]
  id: number
  op: StoreOp
}

export interface StoreReply {
  error?: string
  id: number
  /** Wall time the op took in the thread that ran it. */
  ms: number
  ok: boolean
  result?: unknown
}

export interface TranscriptCacheClient {
  call<T = unknown>(op: StoreOp, args?: unknown[], opts?: { timeoutMs?: number }): Promise<T>
  close(): void
  readonly mode: 'inline' | 'worker'
}

/** The slice of node:worker_threads' Worker the client uses (tests pass their own). */
export interface StoreWorkerPort {
  on(event: 'error' | 'exit' | 'message', listener: (value: any) => void): unknown
  postMessage(message: StoreRequest): void
  terminate(): unknown
}

/**
 * Start the built worker script over one cache file. Its text is read and eval'd rather than
 * loaded by path, so it starts the same from the source tree, an asar and its unpacked dir.
 */
export function spawnStoreWorker(workerFile: string, cacheFile: string): StoreWorkerPort {
  const worker = new Worker(fs.readFileSync(workerFile, 'utf8'), {
    eval: true,
    name: 'transcript-cache',
    workerData: { file: cacheFile }
  })

  worker.unref() // never holds the app open at quit

  return worker
}

export const WORKER_READY_LINE = 'store worker answering: cache I/O runs off the main thread'

/** A store op slower than this is logged with where it ran. */
export const SLOW_STORE_OP_MS = 250
/** A worker call with no timeout of its own gives up (rejects) after this long. */
export const CALL_TIMEOUT_MS = 30_000
/** Worker (re)starts before the client gives up on it for the rest of the run. */
export const MAX_WORKER_STARTS = 3

/** Run one op against a store: the single dispatch both the worker and the inline client use. */
export function runStoreOp(store: TranscriptCacheStore, op: StoreOp, args: readonly unknown[]): unknown {
  if (!STORE_OPS.includes(op)) {
    throw new Error(`unknown transcript cache op: ${String(op)}`)
  }

  if (op === 'metaMany') {
    return (args[0] as Parameters<TranscriptCacheStore['meta']>[0][]).map(key => store.meta(key))
  }

  return (store[op] as (...rest: unknown[]) => unknown)(...args)
}

/** Answer one request (worker side): never throws. */
export function answerStoreRequest(store: TranscriptCacheStore, request: StoreRequest): StoreReply {
  const started = performance.now()

  try {
    const result = runStoreOp(store, request.op, request.args ?? [])

    return { id: request.id, ms: performance.now() - started, ok: true, result }
  } catch (error) {
    return {
      error: (error as Error)?.message || String(error),
      id: request.id,
      ms: performance.now() - started,
      ok: false
    }
  }
}

const slowLine = (op: StoreOp, ms: number, where: string) => `slow store op ${op}: ${Math.round(ms)}ms ${where}`

export function inlineClient(
  store: TranscriptCacheStore,
  log: (line: string) => void = () => undefined
): TranscriptCacheClient {
  let closed = false

  return {
    call: async <T>(op: StoreOp, args: unknown[] = []) => {
      const reply = answerStoreRequest(store, { args, id: 0, op })

      if (reply.ms >= SLOW_STORE_OP_MS) {
        log(slowLine(op, reply.ms, 'ON the main thread (no worker)'))
      }

      if (!reply.ok) {
        throw new Error(reply.error)
      }

      return reply.result as T
    },
    close: () => {
      if (!closed) {
        closed = true
        store.close()
      }
    },
    mode: 'inline'
  }
}

interface Pending {
  op: StoreOp
  reject: (error: Error) => void
  resolve: (value: any) => void
  timer: ReturnType<typeof setTimeout>
}

/**
 * Client over a worker thread that owns the store. A worker that dies is restarted on the
 * next call, MAX_WORKER_STARTS times in all; after that, or when a start throws, `fallback()`
 * (an inline client) takes over, so a broken worker costs smoothness, never the cache.
 */
export function workerClient(deps: {
  fallback: () => TranscriptCacheClient
  log: (line: string) => void
  spawn: () => StoreWorkerPort
}): TranscriptCacheClient {
  const pending = new Map<number, Pending>()
  let port: null | StoreWorkerPort = null
  let starts = 0
  let nextId = 1
  let closed = false
  let inline: null | TranscriptCacheClient = null
  let unusable: Error | null = null
  let announced = false

  const settle = (id: number): Pending | undefined => {
    const entry = pending.get(id)

    if (entry) {
      pending.delete(id)
      clearTimeout(entry.timer)
    }

    return entry
  }

  const lost = (from: StoreWorkerPort, why: string) => {
    if (port !== from) {
      return // an old worker's late exit
    }

    port = null

    if (!closed) {
      deps.log(
        `store worker lost (${why}); ${starts < MAX_WORKER_STARTS ? 'restarting on the next call' : 'falling back to the main thread'}`
      )
    }

    for (const id of [...pending.keys()]) {
      settle(id)?.reject(new Error(`transcript cache worker lost (${why})`))
    }
  }

  const ensurePort = (): null | StoreWorkerPort => {
    if (port) {
      return port
    }

    if (starts >= MAX_WORKER_STARTS) {
      return null
    }

    starts += 1

    try {
      const created = deps.spawn()

      created.on('message', (reply: StoreReply) => {
        const entry = settle(reply?.id)

        if (!entry) {
          return
        }

        if (!announced) {
          announced = true
          deps.log(WORKER_READY_LINE) // once per run: desktop.log shows which thread owns the cache
        }

        if (reply.ms >= SLOW_STORE_OP_MS) {
          deps.log(slowLine(entry.op, reply.ms, 'on the worker thread (main loop not blocked)'))
        }

        if (reply.ok) {
          entry.resolve(reply.result)
        } else {
          entry.reject(new Error(reply.error || 'transcript cache op failed'))
        }
      })
      created.on('error', (error: unknown) => lost(created, (error as Error)?.message || String(error)))
      created.on('exit', (code: unknown) => lost(created, `exit ${String(code)}`))
      port = created
    } catch (error) {
      deps.log(`store worker failed to start: ${(error as Error)?.message || error}`)
    }

    return port
  }

  return {
    call: <T>(op: StoreOp, args: unknown[] = [], opts: { timeoutMs?: number } = {}): Promise<T> => {
      if (closed) {
        return Promise.reject(new Error('transcript cache closed'))
      }

      const target = inline ? null : ensurePort()

      // No worker: a start that threw (no retry: a missing worker file stays missing) or MAX_WORKER_STARTS deaths.
      if (!target) {
        if (!inline && !unusable) {
          deps.log('store worker unavailable: the cache runs on the main thread for the rest of this run')

          try {
            inline = deps.fallback()
          } catch (error) {
            // The file will not open here either (locked, corrupt): no cache this run. Every call rejects,
            // none throws: the renderer's sync read must still get its answer.
            unusable = new Error(`transcript cache unavailable: ${(error as Error)?.message || error}`)
            deps.log(unusable.message)
          }
        }

        return inline ? inline.call<T>(op, args, opts) : Promise.reject(unusable)
      }

      return new Promise<T>((resolve, reject) => {
        const id = nextId++
        // Every call is bounded: a worker stuck on the disk must not leave a prefetch pass pending forever.
        // Its late reply is dropped (the id is settled); the write itself still lands when the disk answers.
        const timeoutMs = opts.timeoutMs ?? CALL_TIMEOUT_MS

        const timer = setTimeout(() => {
          settle(id)?.reject(new Error(`transcript cache ${op} timed out after ${timeoutMs}ms`))
        }, timeoutMs)

        timer.unref?.()
        pending.set(id, { op, reject, resolve, timer })

        try {
          target.postMessage({ args, id, op })
        } catch (error) {
          settle(id)?.reject(error as Error) // e.g. an uncloneable argument
        }
      })
    },
    close: () => {
      if (closed) {
        return
      }

      closed = true

      for (const id of [...pending.keys()]) {
        settle(id)?.reject(new Error('transcript cache closed'))
      }

      // A write in flight is one SQLite transaction: terminating mid-way rolls it back.
      void port?.terminate()
      port = null
      inline?.close()
    },
    get mode() {
      return inline ? ('inline' as const) : ('worker' as const)
    }
  }
}
