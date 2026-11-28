import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it, vi } from 'vitest'

import {
  DISPATCH_PROBE_SKIP_WINDOW_MS,
  ensureHealthyPooledRemoteBackendForDispatch,
  RemoteAnswerLedger
} from './remote-liveness'
import {
  PREWARM_BACKOFF_MS,
  prewarmTargets,
  RECENT_PROFILE_TTL_MS,
  RECENT_PROFILES_PER_CONNECTION,
  RecentRemoteProfiles,
  RemotePrewarmer
} from './remote-prewarm'

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
      listTargets: () => [{ connectionId: 'mac-mini', profile: 'default' }],
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
    expect(ensure).toHaveBeenLastCalledWith({ connectionId: 'mac-mini', profile: 'default' }, { forceProbe: true })
    expect(prewarmer.isWarm('mac-mini')).toBe(false)

    now += PREWARM_BACKOFF_MS[0] - 1
    await prewarmer.tick()
    expect(ensure).toHaveBeenCalledTimes(2) // still backing off

    up = true
    now += 1
    await prewarmer.tick()
    expect(ensure).toHaveBeenCalledTimes(3)
    expect(prewarmer.isWarm('mac-mini')).toBe(true)
    expect(logs.some(line => /mac-mini::default reconnected/.test(line))).toBe(true)
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
      listTargets: () => [
        { connectionId: 'mac-mini', profile: 'default' },
        { connectionId: 'hermes-hub', profile: 'default' }
      ],
      ensure: async (target, opts) => {
        order.push(`${target.connectionId}:${opts.forceProbe}`)
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

    const prewarmer = new RemotePrewarmer({
      listTargets: () => ids.map(connectionId => ({ connectionId, profile: 'default' })),
      ensure,
      log: () => {},
      setTimer: () => ({})
    })

    await prewarmer.start()
    ids = ['mac-mini']
    await prewarmer.tick()

    expect(prewarmer.isWarm('hermes-hub')).toBe(false)
    expect(ensure).toHaveBeenCalledTimes(3)
    prewarmer.stop()
  })
})

describe('recently used named profiles stay warm', () => {
  function store(now: () => number, initial: unknown = null) {
    const saved: any[] = []
    const recent = new RecentRemoteProfiles({
      load: () => initial,
      save: data => saved.push(structuredClone(data)),
      now
    })

    return { recent, saved }
  }

  it('prewarms each connection default plus its named profiles used in the last 24 h', () => {
    let now = 1_000_000
    const { recent, saved } = store(() => now)
    recent.touch('mac-mini', 'bb-dev')
    recent.touch('mac-mini', 'default') // always warm, never recorded
    now += 1000
    recent.touch('mac-mini', 'api-e1628a809db4c3f7')

    expect(prewarmTargets(['mac-mini', 'hermes-hub'], recent)).toEqual([
      { connectionId: 'mac-mini', profile: 'default' },
      { connectionId: 'mac-mini', profile: 'api-e1628a809db4c3f7' },
      { connectionId: 'mac-mini', profile: 'bb-dev' },
      { connectionId: 'hermes-hub', profile: 'default' }
    ])
    expect(saved).toHaveLength(2)

    now += RECENT_PROFILE_TTL_MS
    expect(recent.profilesFor('mac-mini')).toEqual([])
  })

  it('caps the named profiles per connection and survives a restart from the persisted file', () => {
    let now = 0
    const { recent, saved } = store(() => now)

    for (let i = 0; i < RECENT_PROFILES_PER_CONNECTION + 2; i++) {
      now += 120_000
      recent.touch('mac-mini', `p${i}`)
    }

    expect(recent.profilesFor('mac-mini')).toHaveLength(RECENT_PROFILES_PER_CONNECTION)
    expect(recent.profilesFor('mac-mini')[0]).toBe(`p${RECENT_PROFILES_PER_CONNECTION + 1}`)

    const restarted = store(() => now, saved.at(-1)).recent
    expect(restarted.profilesFor('mac-mini')).toEqual(recent.profilesFor('mac-mini'))
    expect(store(() => now, 'garbage').recent.profilesFor('mac-mini')).toEqual([])
  })

  it('the prewarmer holds a named-profile target like a default one', async () => {
    const ensure = vi.fn(async () => {})

    const prewarmer = new RemotePrewarmer({
      listTargets: () => [{ connectionId: 'mac-mini', profile: 'bb-dev' }],
      ensure,
      log: () => {},
      setTimer: () => ({})
    })

    await prewarmer.start()
    await prewarmer.tick()

    expect(ensure).toHaveBeenLastCalledWith({ connectionId: 'mac-mini', profile: 'bb-dev' }, { forceProbe: true })
    expect(prewarmer.isWarm('mac-mini::bb-dev')).toBe(true)
    prewarmer.stop()
  })
})

describe('main.ts wiring', () => {
  // The Electron main process cannot boot under vitest; pin the call sites the behaviour above depends on.
  const main = readFileSync(join(__dirname, 'main.ts'), 'utf8')
  // The warm set (registry SSH filter + prewarmTargets + recent-profile ledger) lives in remote-warm-set.ts.
  const warmSet = readFileSync(join(__dirname, 'remote-warm-set.ts'), 'utf8')

  it('starts the prewarmer when the app is ready and stops it on quit', () => {
    const ready = main.slice(main.indexOf('app.whenReady().then(() => {'))
    expect(ready.indexOf('void remotePrewarmer.start()')).toBeGreaterThan(0)
    expect(main).toMatch(
      /minimizeToTray\.beginQuit\(\)\n\s+mainProcessLagWatchdog\.stop\(\)\n\s+remotePrewarmer\.stop\(\)/
    )
  })

  it('prewarms registry SSH connections through ensureRegistryBackend with a forced keepalive probe', () => {
    // Upstream retired spawn priorities with the local-backend pool (c63d465dbc2).
    expect(main).toMatch(
      /ensureRegistryBackend\(connectionId, profile, '', \{ forceProbe, revalidationTrigger: 'prewarm' \}\)/
    )
    expect(main).toMatch(/createRemoteWarmSet\(\{[^}]*readRegistry: readDesktopConnectionsRegistry,/)
    expect(main).toMatch(/new RemotePrewarmer\(\{\n\s+listTargets: remoteWarmSet\.listTargets,/)
    expect(warmSet).toMatch(/connection\.kind === 'ssh'/)
    expect(warmSet).toMatch(
      /HERMES_DESKTOP_REMOTE_PREWARM === '0' \? \[\] : prewarmTargets\(sshConnectionIds\(\), recent\)/
    )
  })

  it('records a named profile only for a dial flagged as use (never a sweep, prewarm or passive call)', () => {
    // A plain non-passive dial used to count, so a post-reconnect sweep stamped every listed profile
    // (remote-warm-use.test.ts). The gate lives in remote-warm-set.ts noteDial/registryDialCountsAsUse.
    expect(main).toContain('  remoteWarmSet.noteDial(id, profileKey, opts)')
    expect(warmSet).toMatch(/return opts\.use === true && !opts\.passive && !opts\.forceProbe/)
  })

  it('pooled dispatch skips the probe from the shared answer ledger unless forced', () => {
    expect(main).toMatch(
      /answers: remoteAnswerLedger,\n\s+skipWindowMs: opts\.forceProbe \? 0 : DISPATCH_PROBE_SKIP_WINDOW_MS/
    )
    // noteRemoteAnswer moved to slow-link.ts (main.ts is over its line cap); fetchJsonForBackend still calls it on
    // every answer, and it still feeds the shared ledger first.
    expect(
      main.match(/noteRemoteAnswer\(descriptor\.baseUrl, path, Date\.now\(\) - startedAt, rememberLog\)/g)
    ).toHaveLength(2)
    expect(readFileSync(join(__dirname, 'slow-link.ts'), 'utf8')).toMatch(
      /export function noteRemoteAnswer\(.*\): void \{\n\s+remoteAnswerLedger\.recordAnswer\(baseUrl\)/
    )
  })
})
