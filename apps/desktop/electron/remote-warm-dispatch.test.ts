import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it, vi } from 'vitest'

import {
  DISPATCH_PROBE_SKIP_WINDOW_MS,
  ensureHealthyPooledRemoteBackendForDispatch,
  RemoteAnswerLedger
} from './remote-liveness'
import { PREWARM_BACKOFF_MS, RemotePrewarmer } from './remote-prewarm'

// Part B (instant session loads): Desktop prewarms every SSH gateway at startup, holds it with a forced keepalive
// probe, reconnects when the tunnel drops, and skips the per-dispatch /api/health probe while the backend answered
// within the last 30 s.

const BASE_URL = 'http://127.0.0.1:50001'

function dispatchDeps(answers: RemoteAnswerLedger, over: any = {}) {
  const connection = { baseUrl: BASE_URL, mode: 'ssh' }
  const connectionPromise = Promise.resolve(connection)

  return {
    connection,
    probe: vi.fn(async () => ({ ok: true })),
    reconnect: vi.fn(async () => ({ baseUrl: 'http://127.0.0.1:50002', mode: 'ssh' })),
    retire: vi.fn(async () => {}),
    opts: { connectionPromise, currentConnectionPromise: () => connectionPromise, answers, ...over }
  }
}

describe('dispatch probe skip', () => {
  it('skips the probe when the backend answered within 30 s', async () => {
    let now = 1_000_000
    const answers = new RemoteAnswerLedger(() => now)
    answers.recordAnswer(BASE_URL)
    now += DISPATCH_PROBE_SKIP_WINDOW_MS - 1
    const d = dispatchDeps(answers)
    const onProbe = vi.fn()

    const result = await ensureHealthyPooledRemoteBackendForDispatch({ ...d.opts, ...d, onProbe })

    expect(result).toBe(d.connection)
    expect(d.probe).not.toHaveBeenCalled()
    expect(onProbe).toHaveBeenCalledWith('skipped', expect.any(Number))
  })

  it('runs the probe once 30 s have passed, and a success re-arms the skip', async () => {
    let now = 1_000_000
    const answers = new RemoteAnswerLedger(() => now)
    answers.recordAnswer(BASE_URL)
    now += DISPATCH_PROBE_SKIP_WINDOW_MS
    const d = dispatchDeps(answers)

    await ensureHealthyPooledRemoteBackendForDispatch({ ...d.opts, ...d })

    expect(d.probe).toHaveBeenCalledWith(d.connection, '/api/health', expect.anything())
    expect(answers.answeredWithin(BASE_URL, DISPATCH_PROBE_SKIP_WINDOW_MS)).toBe(true)
  })

  it('never skips for a backend that has not answered', async () => {
    const d = dispatchDeps(new RemoteAnswerLedger())

    await ensureHealthyPooledRemoteBackendForDispatch({ ...d.opts, ...d })

    expect(d.probe).toHaveBeenCalledTimes(1)
  })
})

describe('reconnect after a dropped tunnel', () => {
  it('a forced keepalive probe ignores a recent answer, retires the dead tunnel and reconnects', async () => {
    const answers = new RemoteAnswerLedger()
    answers.recordAnswer(BASE_URL)
    const d = dispatchDeps(answers, { skipWindowMs: 0 })
    d.probe.mockRejectedValue(new Error('socket hang up'))

    const result = await ensureHealthyPooledRemoteBackendForDispatch({ ...d.opts, ...d })

    expect(d.probe).toHaveBeenCalled()
    expect(d.retire).toHaveBeenCalledTimes(1)
    expect(d.reconnect).toHaveBeenCalledTimes(1)
    expect(result.baseUrl).toBe('http://127.0.0.1:50002')
    expect(answers.answeredWithin(BASE_URL, DISPATCH_PROBE_SKIP_WINDOW_MS)).toBe(false)
  })

  it('the prewarmer retries a connection that dropped, with backoff, until it is warm again', async () => {
    let now = 0
    const logs: string[] = []
    let up = true

    const ensure = vi.fn(async () => {
      if (!up) {
        throw new Error('ssh: connect to host mini: Connection refused')
      }
    })

    const prewarmer = new RemotePrewarmer({
      listConnectionIds: () => ['mac-mini'],
      ensure,
      log: line => logs.push(line),
      now: () => now,
      setTimer: () => ({})
    })

    await prewarmer.start()
    expect(prewarmer.isWarm('mac-mini')).toBe(true)

    up = false
    now += 15_000
    await prewarmer.tick()
    expect(ensure).toHaveBeenLastCalledWith('mac-mini', { forceProbe: true })
    expect(prewarmer.isWarm('mac-mini')).toBe(false)

    now += PREWARM_BACKOFF_MS[0] - 1
    await prewarmer.tick()
    expect(ensure).toHaveBeenCalledTimes(2) // still backing off

    up = true
    now += 1
    await prewarmer.tick()
    expect(ensure).toHaveBeenCalledTimes(3)
    expect(prewarmer.isWarm('mac-mini')).toBe(true)
    expect(logs.some(line => /mac-mini reconnected/.test(line))).toBe(true)
    prewarmer.stop()
  })
})

describe('prewarm at startup', () => {
  it('dials every SSH connection in parallel at start, then keeps them on a timer', async () => {
    const order: string[] = []
    const timers: number[] = []
    let release!: () => void
    const gate = new Promise<void>(resolve => (release = resolve))

    const prewarmer = new RemotePrewarmer({
      listConnectionIds: () => ['mac-mini', 'hermes-hub'],
      ensure: async (id, opts) => {
        order.push(`${id}:${opts.forceProbe}`)
        await gate
      },
      log: () => {},
      setTimer: (_fn, ms) => (timers.push(ms), {}),
      tickMs: 15_000
    })

    const started = prewarmer.start()
    await Promise.resolve()
    expect(order).toEqual(['mac-mini:false', 'hermes-hub:false']) // both dials in flight together
    release()
    await started

    expect(prewarmer.isWarm('mac-mini') && prewarmer.isWarm('hermes-hub')).toBe(true)
    expect(timers).toEqual([15_000])
    prewarmer.stop()
    expect(prewarmer.running).toBe(false)
  })

  it('drops a connection removed from connections.json', async () => {
    let ids = ['mac-mini', 'hermes-hub']
    const ensure = vi.fn(async () => {})
    const prewarmer = new RemotePrewarmer({ listConnectionIds: () => ids, ensure, log: () => {}, setTimer: () => ({}) })

    await prewarmer.start()
    ids = ['mac-mini']
    await prewarmer.tick()

    expect(prewarmer.isWarm('hermes-hub')).toBe(false)
    expect(ensure).toHaveBeenCalledTimes(3)
    prewarmer.stop()
  })
})

describe('main.ts wiring', () => {
  // The Electron main process cannot boot under vitest; pin the call sites the behaviour above depends on.
  const main = readFileSync(join(__dirname, 'main.ts'), 'utf8')

  it('starts the prewarmer when the app is ready and stops it on quit', () => {
    const ready = main.slice(main.indexOf('app.whenReady().then(() => {'))
    expect(ready.indexOf('void remotePrewarmer.start()')).toBeGreaterThan(0)
    expect(main).toMatch(/minimizeToTray\.beginQuit\(\)\n\s+mainProcessLagWatchdog\.stop\(\)\n\s+remotePrewarmer\.stop\(\)/)
  })

  it('prewarms registry SSH connections through ensureRegistryBackend with a forced keepalive probe', () => {
    expect(main).toMatch(/connection\.kind === 'ssh'/)
    expect(main).toMatch(/ensureRegistryBackend\(connectionId, 'default', '', \{ forceProbe, spawnPriority: 'background' \}\)/)
  })

  it('pooled dispatch skips the probe from the shared answer ledger unless forced', () => {
    expect(main).toMatch(/answers: remoteAnswerLedger,\n\s+skipWindowMs: opts\.forceProbe \? 0 : DISPATCH_PROBE_SKIP_WINDOW_MS/)
    expect(main).toMatch(/function noteRemoteAnswer\(baseUrl: string, path: string\) \{\n\s+remoteAnswerLedger\.recordAnswer\(baseUrl\)/)
  })
})
