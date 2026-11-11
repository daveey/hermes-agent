import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { Worker } from 'node:worker_threads'

import { build } from 'esbuild'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'

import { TranscriptCacheStore } from './transcript-cache'
import {
  inlineClient,
  MAX_WORKER_STARTS,
  type StoreWorkerPort,
  type TranscriptCacheClient,
  WORKER_READY_LINE,
  workerClient
} from './transcript-cache-client'
import { installTranscriptCache, registrySnapshot } from './transcript-cache-host'

const BLOCK_MS = 1_000

/** A worker that speaks the store protocol but holds its own thread for BLOCK_MS per request (a stuck disk). */
const BLOCKING_WORKER = `
const { parentPort } = require('node:worker_threads')
parentPort.on('message', request => {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ${BLOCK_MS})
  parentPort.postMessage({ id: request.id, ms: ${BLOCK_MS}, ok: true, result: { bytes: 1, sessions: 1 } })
})
`

const key = { connectionId: 'mac-mini', profile: 'default', sessionId: 's1' }
const rows = [{ content: 'hello', id: 1, role: 'user' }]

let root = ''
let workerSource = ''
const clients: TranscriptCacheClient[] = []
const workers: Worker[] = []

beforeAll(async () => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'transcript-cache-client-'))
  const outfile = path.join(root, 'worker.js')

  // The shipped worker bundle's shape (scripts/bundle-electron-main.mjs): one CJS file, node builtins only.
  await build({
    bundle: true,
    entryPoints: [path.join(import.meta.dirname, 'transcript-cache-worker.ts')],
    format: 'cjs',
    logLevel: 'silent',
    outfile,
    platform: 'node',
    target: 'node20'
  })
  workerSource = fs.readFileSync(outfile, 'utf8')
})

afterEach(async () => {
  for (const client of clients.splice(0)) {
    client.close()
  }

  await Promise.all(workers.splice(0).map(worker => worker.terminate()))
})

afterAll(() => {
  fs.rmSync(root, { force: true, recursive: true })
})

const spawnSource = (source: string, file = ''): StoreWorkerPort => {
  const worker = new Worker(source, { eval: true, workerData: { file } })
  workers.push(worker)

  return worker
}

const cacheFile = (name: string) => path.join(root, `${name}.sqlite`)

/** Longest gap between two turns of this thread's event loop while `work` is pending. */
async function longestLoopGap(work: () => Promise<unknown>): Promise<number> {
  let last = performance.now()
  let longest = 0

  const timer = setInterval(() => {
    const now = performance.now()
    longest = Math.max(longest, now - last)
    last = now
  }, 10)

  try {
    await new Promise(resolve => setTimeout(resolve, 30)) // the interval is running before the work starts
    await work()
    await new Promise(resolve => setTimeout(resolve, 30)) // a blocked loop reports its gap on the next turn
  } finally {
    clearInterval(timer)
  }

  return longest
}

describe('[daveey] transcript cache store off the main thread', () => {
  it('a store op that blocks for a second on the worker never stalls the calling event loop', async () => {
    const logs: string[] = []

    const client = workerClient({
      fallback: () => {
        throw new Error('no fallback expected')
      },
      log: line => logs.push(line),
      spawn: () => spawnSource(BLOCKING_WORKER)
    })

    clients.push(client)

    let result: unknown
    const started = performance.now()

    const gap = await longestLoopGap(async () => {
      result = await client.call('stats')
    })

    expect(result).toEqual({ bytes: 1, sessions: 1 })
    expect(performance.now() - started).toBeGreaterThanOrEqual(BLOCK_MS - 50) // the op really took its second
    expect(gap).toBeLessThan(BLOCK_MS / 2) // ...and this thread kept turning the whole time
    expect(logs).toEqual([
      WORKER_READY_LINE,
      `slow store op stats: ${BLOCK_MS}ms on the worker thread (main loop not blocked)`
    ])
  })

  it('control: the same blocking op through the in-thread client stalls the loop for the whole op', async () => {
    const logs: string[] = []

    const blockingStore = {
      close: () => undefined,
      stats: () => {
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, BLOCK_MS)

        return { bytes: 1, sessions: 1 }
      }
    } as unknown as TranscriptCacheStore

    const client = inlineClient(blockingStore, line => logs.push(line))
    const gap = await longestLoopGap(() => client.call('stats'))

    expect(gap).toBeGreaterThanOrEqual(BLOCK_MS - 50)
    expect(logs).toHaveLength(1)
    expect(logs[0]).toMatch(/^slow store op stats: \d+ms ON the main thread \(no worker\)$/)
  })

  it('the real worker bundle owns the SQLite file: writes, reads and batched meta round-trip through it', async () => {
    const file = cacheFile('real')

    const client = workerClient({
      fallback: () => {
        throw new Error('no fallback expected')
      },
      log: () => undefined,
      spawn: () => spawnSource(workerSource, file)
    })

    clients.push(client)

    expect(await client.call('putLatest', [key, rows, { activity: 5, messageCount: 1 }])).toBe(true)
    expect(await client.call('read', [key])).toMatchObject({ activity: 5, messageCount: 1, messages: rows })
    expect(await client.call('metaMany', [[key, { ...key, sessionId: 'absent' }]])).toEqual([
      expect.objectContaining({ activity: 5, messageCount: 1 }),
      null
    ])
    expect(await client.call('stats')).toMatchObject({ sessions: 1 })
    await client.call('setMeta', ['hub-gateway:mini', 'mac-mini'])
    expect(await client.call('getMeta', ['hub-gateway:mini'])).toBe('mac-mini')
    await expect(client.call('nope' as never)).rejects.toThrow(/unknown transcript cache op/)
    expect(client.mode).toBe('worker')

    // Persisted by the worker: a fresh in-thread store over the same file sees the entry.
    client.close()
    await Promise.all(workers.splice(0).map(worker => worker.terminate()))
    const reopened = new TranscriptCacheStore(file)
    expect(reopened.read(key)?.messages).toEqual(rows)
    reopened.close()
  })

  it('a call past its timeout rejects instead of waiting on a busy worker', async () => {
    const client = workerClient({
      fallback: () => {
        throw new Error('no fallback expected')
      },
      log: () => undefined,
      spawn: () => spawnSource(BLOCKING_WORKER)
    })

    clients.push(client)

    await expect(client.call('read', [key], { timeoutMs: 50 })).rejects.toThrow(/read timed out after 50ms/)
  })

  it(`a worker that keeps dying is restarted ${MAX_WORKER_STARTS} times, then the in-thread store takes over`, async () => {
    const logs: string[] = []
    const file = cacheFile('dying')
    let spawned = 0

    const client = workerClient({
      fallback: () => inlineClient(new TranscriptCacheStore(file)),
      log: line => logs.push(line),
      spawn: () => {
        spawned += 1

        return spawnSource(
          `require('node:worker_threads').parentPort.on('message', () => { throw new Error('boom ${spawned}') })`
        )
      }
    })

    clients.push(client)

    for (let attempt = 1; attempt <= MAX_WORKER_STARTS; attempt += 1) {
      await expect(client.call('stats')).rejects.toThrow(/worker lost \(boom/)
      expect(spawned).toBe(attempt)
    }

    // No fourth worker: the cache keeps working, in this thread.
    expect(await client.call('putLatest', [key, rows])).toBe(true)
    expect(await client.call('read', [key])).toMatchObject({ messages: rows })
    expect(spawned).toBe(MAX_WORKER_STARTS)
    expect(client.mode).toBe('inline')
    expect(logs.at(-1)).toBe('store worker unavailable: the cache runs on the main thread for the rest of this run')
  })

  it('a spawn that throws (no worker file) falls back at once, without retrying', async () => {
    const logs: string[] = []
    let attempts = 0

    const client = workerClient({
      fallback: () => inlineClient(new TranscriptCacheStore(cacheFile('nospawn'))),
      log: line => logs.push(line),
      spawn: () => {
        attempts += 1

        throw new Error('no worker file')
      }
    })

    clients.push(client)

    expect(await client.call('stats')).toEqual({ bytes: 0, sessions: 0 })
    expect(await client.call('stats')).toEqual({ bytes: 0, sessions: 0 })
    expect(attempts).toBe(1)
    expect(client.mode).toBe('inline')
    expect(logs).toEqual([
      'store worker failed to start: no worker file',
      'store worker unavailable: the cache runs on the main thread for the rest of this run'
    ])
  })
})

describe('[daveey] transcript cache host with a store worker', () => {
  function install(spawnWorker: (file: string) => StoreWorkerPort) {
    const dir = fs.mkdtempSync(path.join(root, 'host-'))
    const on = new Map<string, (event: any, payload?: any) => void>()
    const handle = new Map<string, () => unknown>()
    const logs: string[] = []

    const getJson = async (descriptor: any, requestPath: string) => {
      if (requestPath.startsWith('/api/plugins/hub-sync/sessions')) {
        return {
          sessions: [
            { activity: 2, gateway: 'mini', id: 'm1', message_count: 3, profile: 'default' },
            { activity: 1, gateway: 'macbook', id: 'l1', message_count: 3, profile: 'default' }
          ]
        }
      }

      if (requestPath === '/api/plugins/hub-sync/status') {
        return { gateway: descriptor.connectionId === 'local' ? 'macbook' : 'mini' }
      }

      return { messages: [{ content: `row of ${descriptor.connectionId}`, id: 1, role: 'user' }] }
    }

    const host = installTranscriptCache({
      backendFor: async (connectionId: string) => ({ connectionId }),
      getJson,
      ipcMain: {
        handle: (channel: string, fn: () => unknown) => handle.set(channel, fn),
        on: (channel: string, fn: any) => on.set(channel, fn)
      } as never,
      log: line => logs.push(line),
      registry: () => ({ connectionIds: ['local', 'mac-mini'], primary: 'local' }),
      spawnWorker,
      userDataDir: dir
    })

    clients.push(host.client)

    return { dir, handle, host, logs, on }
  }

  /** A sync-IPC event: resolves when the handler sets returnValue. */
  const syncEvent = () => {
    let settle: (value: unknown) => void = () => undefined
    const replied = new Promise(resolve => (settle = resolve))
    let sets = 0

    const event = {
      get sets() {
        return sets
      },
      set returnValue(value: unknown) {
        sets += 1
        settle(value)
      }
    }

    return { event, replied }
  }

  it('prefetch, renderer writes and the sync read all go through the worker; this thread never opens the file', async () => {
    const { handle, host, on } = install(file => spawnSource(workerSource, file))

    expect(host.store).toBeNull() // no in-thread SQLite handle exists
    expect(await host.prefetcher.tick()).toMatchObject({ failed: 0, fetched: 2 })
    expect(await host.prefetcher.tick()).toMatchObject({ fetched: 0, skipped: 2 }) // freshness read back from the worker

    const read = syncEvent()
    on.get('hermes:transcript-cache:read')!(read.event, { connectionId: '', profile: '', sessionId: 'l1' })
    expect(read.event.sets).toBe(0) // answered when the worker replies, not inside the listener
    expect(await read.replied).toMatchObject({ messages: [{ content: 'row of local', id: 1, role: 'user' }] })
    expect(read.event.sets).toBe(1)

    on.get('hermes:transcript-cache:put')!({}, { key, kind: 'latest', messages: rows })
    const after = syncEvent()
    on.get('hermes:transcript-cache:read')!(after.event, key) // queued behind the put on the worker
    expect(await after.replied).toMatchObject({ messages: rows })
    expect(await handle.get('hermes:transcript-cache:stats')!()).toMatchObject({ sessions: 3 })
    expect(host.store).toBeNull()
    expect(host.client.mode).toBe('worker')
    host.stop()
  })

  it('a prefetch pass whose cache writes each take a second leaves this event loop turning', async () => {
    // The stall this guards: on the main thread, one prefetch write that waited on the disk froze the app 1-1.8 s.
    const slowWrites = `require('node:worker_threads').parentPort.on('message', request => {
      if (request.op === 'putLatest') Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ${BLOCK_MS})
    })\n`

    const { host, logs } = install(file => spawnSource(slowWrites + workerSource, file))
    let result: unknown
    const started = performance.now()

    const gap = await longestLoopGap(async () => {
      result = await host.prefetcher.tick()
    })

    expect(result).toMatchObject({ failed: 0, fetched: 2 })
    expect(performance.now() - started).toBeGreaterThanOrEqual(BLOCK_MS - 50) // the writes really were slow
    expect(gap).toBeLessThan(BLOCK_MS / 2)
    expect(logs).toEqual([WORKER_READY_LINE, 'transcript prefetch: 2 fetched, 0 failed, 0 current of 2'])
    host.stop()
  })

  it('given the built worker file (what main.ts passes), the host starts the worker itself', async () => {
    const dir = fs.mkdtempSync(path.join(root, 'host-file-'))
    const logs: string[] = []

    const host = installTranscriptCache({
      backendFor: async (connectionId: string) => ({ connectionId }),
      getJson: async () => ({}),
      ipcMain: { handle: () => undefined, on: () => undefined } as never,
      log: line => logs.push(line),
      registry: () => registrySnapshot({ connections: [{ id: 'local' }], primary: 'local' }),
      userDataDir: dir,
      workerFile: path.join(root, 'worker.js')
    })

    clients.push(host.client)

    expect(await host.client.call('putLatest', [key, rows])).toBe(true)
    expect(await host.client.call('stats')).toMatchObject({ sessions: 1 })
    expect(host.client.mode).toBe('worker')
    expect(host.store).toBeNull()
    expect(logs).toEqual([WORKER_READY_LINE])
    host.stop()
  })

  it('a missing worker file costs the worker, not the cache: the store runs in this thread and says so', async () => {
    const dir = fs.mkdtempSync(path.join(root, 'host-nofile-'))
    const logs: string[] = []

    const host = installTranscriptCache({
      backendFor: async (connectionId: string) => ({ connectionId }),
      getJson: async () => ({}),
      ipcMain: { handle: () => undefined, on: () => undefined } as never,
      log: line => logs.push(line),
      registry: () => ({ connectionIds: ['local'], primary: 'local' }),
      userDataDir: dir,
      workerFile: path.join(root, 'absent.js')
    })

    clients.push(host.client)

    expect(await host.client.call('putLatest', [key, rows])).toBe(true)
    expect(host.client.mode).toBe('inline')
    expect(host.store?.read(key)?.messages).toEqual(rows)
    expect(logs[0]).toMatch(/^store worker failed to start: ENOENT/)
    expect(logs[1]).toBe('store worker unavailable: the cache runs on the main thread for the rest of this run')
    host.stop()
  })

  it('neither the worker nor the in-thread store can open the file: every call rejects, the sync read answers null', async () => {
    const dir = fs.mkdtempSync(path.join(root, 'host-broken-'))
    const logs: string[] = []
    const on = new Map<string, (event: any, payload?: any) => void>()
    // The cache path is a directory: SQLite cannot open it, on any thread.
    fs.mkdirSync(path.join(dir, 'transcript-cache.sqlite'))

    const host = installTranscriptCache({
      backendFor: async (connectionId: string) => ({ connectionId }),
      getJson: async () => ({}),
      ipcMain: { handle: () => undefined, on: (channel: string, fn: any) => on.set(channel, fn) } as never,
      log: line => logs.push(line),
      registry: () => ({ connectionIds: ['local'], primary: 'local' }),
      userDataDir: dir,
      workerFile: path.join(root, 'absent.js')
    })

    clients.push(host.client)

    const read = syncEvent()
    on.get('hermes:transcript-cache:read')!(read.event, key) // must not throw out of the listener
    expect(await read.replied).toBeNull()
    on.get('hermes:transcript-cache:put')!({}, { key, kind: 'latest', messages: rows })
    await expect(host.client.call('stats')).rejects.toThrow(/^transcript cache unavailable: /)
    // Opened (and reported) once, not on every call.
    expect(logs.filter(line => line.startsWith('transcript cache unavailable: '))).toHaveLength(1)
    expect(logs.filter(line => line.startsWith('store worker unavailable'))).toHaveLength(1)
    host.stop()
  })

  it('the sync read always answers: null for a bad key, a miss, and a worker that cannot reply', async () => {
    const { host, logs, on } = install(() =>
      spawnSource(`require('node:worker_threads').parentPort.on('message', () => { throw new Error('disk gone') })`)
    )

    const bad = syncEvent()
    on.get('hermes:transcript-cache:read')!(bad.event, { sessionId: '' })
    expect(await bad.replied).toBeNull()

    const failed = syncEvent()
    on.get('hermes:transcript-cache:read')!(failed.event, key)
    expect(await failed.replied).toBeNull()
    expect(failed.event.sets).toBe(1)
    expect(logs.some(line => /transcript cache read failed: .*worker lost \(disk gone\)/.test(line))).toBe(true)
    host.stop()
  })
})
