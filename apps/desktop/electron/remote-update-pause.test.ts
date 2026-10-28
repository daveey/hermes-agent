import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { RemotePrewarmer } from './remote-prewarm'
import { RemoteUpdatePause, remoteUpdatePauseKey, UPDATE_PAUSE_BACKOFF_MS } from './remote-update-pause'

// 2026-10-27 20:58–21:00 PDT: the hub ran its own `hermes update`; every renderer request and prewarm tick dialed a
// new SSH control master, hit the remote preflight's "SSH startup is paused" refusal and closed it again.

const HUB = { user: 'ubuntu', host: '192.0.2.10', port: 22, remoteHermesPath: '' }
const KEY = remoteUpdatePauseKey(HUB)

function updateRefusal(): Error {
  const error: any = new Error('Remote Hermes update process 1219818 is still running; SSH startup is paused.')
  error.kind = 'update-in-progress'

  return error
}

// A remote host plus the slice of main.ts the requests go through: ensureRegistryBackend() pools one entry per
// (connection, profile) while its dial is in flight and drops it when the dial fails; the dial itself is
// bootstrapSshConnection() wrapped in the pause gate. `dials` counts SSH control masters opened.
function harness() {
  let now = 1_000_000
  const logs: string[] = []
  const remote = { updating: true, dials: [] as number[] }
  const pause = new RemoteUpdatePause({ log: line => logs.push(line), now: () => now })
  const pool = new Map<string, Promise<{ baseUrl: string }>>()

  const dial = async () => {
    remote.dials.push(now)
    await Promise.resolve()

    if (remote.updating) {
      throw updateRefusal()
    }

    return { baseUrl: 'http://127.0.0.1:50001' }
  }

  const ensure = (connectionId: string, profile: string) => {
    const poolKey = `conn:${connectionId}::${profile}`
    const existing = pool.get(poolKey)

    if (existing) {
      return existing
    }

    const entry = pause.run(KEY, dial).catch(error => {
      if (pool.get(poolKey) === entry) {
        pool.delete(poolKey)
      }

      throw error
    })

    pool.set(poolKey, entry)

    return entry
  }

  return {
    advance: (ms: number) => (now += ms),
    ensure,
    logs,
    now: () => now,
    pause,
    remote
  }
}

// The fewest dials a backoff schedule allows over `spanMs`: the first refusal, then one probe per window.
function dialsAllowed(spanMs: number): number {
  let at = 0
  let count = 1

  for (let i = 0; ; i++) {
    at += UPDATE_PAUSE_BACKOFF_MS[Math.min(i, UPDATE_PAUSE_BACKOFF_MS.length - 1)]

    if (at > spanMs) {
      return count
    }

    count += 1
  }
}

// Before the first refusal nothing knows the host is updating, so parallel first dials (one per profile) may share
// t0. After it, probes are one per window, each window at least the next backoff step.
function expectSpacedByBackoff(dials: number[]) {
  const probes = dials.filter(at => at !== dials[0])
  expect(dials.length - probes.length).toBeLessThanOrEqual(2)

  for (let i = 0; i < probes.length; i++) {
    expect(probes[i] - (i ? probes[i - 1] : dials[0])).toBeGreaterThanOrEqual(
      UPDATE_PAUSE_BACKOFF_MS[Math.min(i, UPDATE_PAUSE_BACKOFF_MS.length - 1)]
    )
  }
}

describe('renderer API requests during a remote update', () => {
  it('fail fast from the cached refusal, dial at most once per backoff window, and recover once', async () => {
    const h = harness()
    const outcomes: string[] = []

    // Two minutes of renderer traffic like the incident: a session load, the kanban boards poll and a
    // session-kanban lookup every second, each its own pool key.
    for (let second = 0; second < 120; second++) {
      const results = await Promise.allSettled([
        h.ensure('hermes-hub', 'default'),
        h.ensure('hermes-hub', 'default'),
        h.ensure('hermes-hub', 'bb-dev')
      ])

      for (const result of results) {
        outcomes.push(result.status === 'rejected' ? (result.reason as any).message : 'ok')
      }

      h.advance(1_000)
    }

    expect(outcomes).toHaveLength(360)
    expect(outcomes.every(message => /SSH startup is paused/.test(message))).toBe(true)
    expect(h.remote.dials.length).toBeLessThanOrEqual(dialsAllowed(120_000) + 1) // + the parallel bb-dev first dial
    expect(h.remote.dials.length).toBeLessThan(10) // 360 requests, not 360 (or 60) SSH dials
    expectSpacedByBackoff(h.remote.dials)
    expect(h.pause.paused(KEY)).toBe(true)

    // The update ends: the next request after the window probes once, succeeds, and the connection is back.
    h.remote.updating = false
    const dialsBefore = h.remote.dials.length
    h.advance(UPDATE_PAUSE_BACKOFF_MS.at(-1)!)
    await expect(h.ensure('hermes-hub', 'default')).resolves.toEqual({ baseUrl: 'http://127.0.0.1:50001' })
    await expect(h.ensure('hermes-hub', 'default')).resolves.toEqual({ baseUrl: 'http://127.0.0.1:50001' })
    expect(h.remote.dials.length).toBe(dialsBefore + 1)
    expect(h.pause.paused(KEY)).toBe(false)
    expect(h.logs.filter(line => /remote update finished; reconnected/.test(line))).toHaveLength(1)

    // Other profiles dial normally again once the pause is gone.
    await expect(h.ensure('hermes-hub', 'bb-dev')).resolves.toBeTruthy()
    expect(h.remote.dials.length).toBe(dialsBefore + 2)
  })

  it('the paused error keeps the refusal text and says when the next probe may run', async () => {
    const h = harness()
    await expect(h.ensure('hermes-hub', 'default')).rejects.toThrow(/process 1219818/)

    const error: any = await h.ensure('hermes-hub', 'default').catch(e => e)
    expect(error.kind).toBe('update-in-progress')
    expect(error.code).toBe('remote-update-paused')
    expect(error.retryAt).toBe(h.now() + UPDATE_PAUSE_BACKOFF_MS[0])
    expect(h.remote.dials).toHaveLength(1)
  })

  it('a probe that fails some other way ends the pause instead of masking the new failure', async () => {
    const h = harness()
    await h.ensure('hermes-hub', 'default').catch(() => {})
    h.advance(UPDATE_PAUSE_BACKOFF_MS[0])

    await expect(
      h.pause.run(KEY, async () => {
        throw new Error('ssh: connect to host 192.0.2.10 port 22: Connection refused')
      })
    ).rejects.toThrow(/Connection refused/)
    expect(h.pause.paused(KEY)).toBe(false)
  })
})

describe('remote-prewarm during a remote update', () => {
  it('follows the pause window instead of redialing, shares it with renderer requests, and reconnects once', async () => {
    const h = harness()
    const prewarmLogs: string[] = []

    const prewarmer = new RemotePrewarmer({
      listTargets: () => [{ connectionId: 'hermes-hub', profile: 'default' }],
      ensure: target => h.ensure(target.connectionId, target.profile),
      log: line => prewarmLogs.push(line),
      now: h.now,
      setTimer: () => ({})
    })

    await prewarmer.start() // first round hits the refusal
    expect(h.remote.dials).toHaveLength(1)
    expect(prewarmLogs.at(-1)).toMatch(/retry 1 in 2000 ms/)

    // Three minutes: prewarm ticks every 15 s and the renderer keeps asking every second in between.
    for (let second = 1; second <= 180; second++) {
      h.advance(1_000)
      await h.ensure('hermes-hub', 'default').catch(() => {})

      if (second % 15 === 0) {
        await prewarmer.tick()
      }
    }

    expect(h.remote.dials.length).toBeLessThanOrEqual(dialsAllowed(181_000))
    expectSpacedByBackoff(h.remote.dials)
    expect(prewarmer.isWarm('hermes-hub')).toBe(false)

    // No renderer traffic now: prewarm alone is the probe that sees the update finish.
    h.remote.updating = false
    const dialsBefore = h.remote.dials.length

    for (let i = 0; i < 4 && !prewarmer.isWarm('hermes-hub'); i++) {
      h.advance(15_000)
      await prewarmer.tick()
    }

    expect(prewarmer.isWarm('hermes-hub')).toBe(true)
    expect(h.remote.dials.length).toBe(dialsBefore + 1)
    expect(prewarmLogs.filter(line => /hermes-hub::default reconnected/.test(line))).toHaveLength(1)
    expect(h.logs.filter(line => /remote update finished; reconnected/.test(line))).toHaveLength(1)
    prewarmer.stop()
  })
})

describe('main.ts wiring', () => {
  // The Electron main process cannot boot under vitest; pin the chokepoint the behaviour above depends on.
  const main = readFileSync(join(__dirname, 'main.ts'), 'utf8')
  const bootstrap = main.slice(main.indexOf('async function bootstrapSshConnection('))
  const body = bootstrap.slice(0, bootstrap.indexOf('\n}\n'))

  it('every non-managed SSH dial goes through the pause gate, keyed by the remote install', () => {
    expect(body).toMatch(/return remoteUpdatePause\.run\(remoteUpdatePauseKey\(resolvedConfig\), start\)/)
    expect(body).toMatch(/if \(metadata\?\.managedUpdateCorrelation\) \{\n\s+return start\(\)/)
    expect(main).toMatch(/const remoteUpdatePause = new RemoteUpdatePause\(/)
  })
})
