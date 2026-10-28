import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  isRetryableRemoteBootFailure,
  isSshAuthFailedBootFailure,
  isSshClientFailedBootFailure,
  sshClientFailedError
} from './backend-start-failure'
import { RemotePrewarmer } from './remote-prewarm'
import {
  classifyRemoteDialFailure,
  RemoteUpdatePause,
  remoteUpdatePauseKey,
  UNREACHABLE_BACKOFF_MS
} from './remote-update-pause'

// 2026-10-27 16:53–16:55 PDT: the mini stopped answering on its tailnet address. Every renderer request opened its
// own SSH control master and waited out the 15 s SSH timeout; the next request dialed again the moment one failed:
// 11 dials and 36 stalled requests in 2 minutes. remote-update-pause.ts now remembers a down remote the same way it
// remembers a remote's own update.

const MINI = { user: 'daveey', host: '192.0.2.20', port: 22, remoteHermesPath: '' }
const KEY = remoteUpdatePauseKey(MINI)

function sshFailure(kind: string, message: string): Error {
  const error: any = new Error(message)
  error.kind = kind

  return error
}

const timeout = () =>
  sshFailure(
    'timeout',
    'SSH operation to daveey@192.0.2.20 timed out. The connection may be half-open (e.g. after sleep); reconnecting.'
  )

// A remote host plus the slice of main.ts the requests go through: ensureRegistryBackend() pools one entry per
// (connection, profile) while its dial is in flight and drops it when the dial fails; the dial itself is
// bootstrapSshConnection(), which runs through the gate unless the caller is a managed restore (a correlation id:
// the registry Reconnect action and managed updates). `dials` counts SSH control masters opened.
function harness(failure: () => Error = timeout) {
  let now = 1_000_000
  const logs: string[] = []
  const remote = { down: true, dials: [] as number[], failure }
  const gate = new RemoteUpdatePause({ log: line => logs.push(line), now: () => now })
  const pool = new Map<string, Promise<{ baseUrl: string }>>()

  const dial = async () => {
    remote.dials.push(now)
    await Promise.resolve()

    if (remote.down) {
      throw remote.failure()
    }

    return { baseUrl: 'http://127.0.0.1:50002' }
  }

  const ensure = (connectionId: string, profile: string, managedUpdateCorrelation = '') => {
    const poolKey = `conn:${connectionId}::${profile}`
    const existing = pool.get(poolKey)

    if (existing) {
      return existing
    }

    const entry = (managedUpdateCorrelation ? dial() : gate.run(KEY, dial)).catch(error => {
      if (pool.get(poolKey) === entry) {
        pool.delete(poolKey)
      }

      throw error
    })

    pool.set(poolKey, entry)

    return entry
  }

  return { advance: (ms: number) => (now += ms), ensure, gate, logs, now: () => now, remote }
}

// The most dials the unreachable schedule allows over `spanMs`: the first failure, then one probe per window.
function dialsAllowed(spanMs: number): number {
  let at = 0
  let count = 1

  for (let i = 0; ; i++) {
    at += UNREACHABLE_BACKOFF_MS[Math.min(i, UNREACHABLE_BACKOFF_MS.length - 1)]

    if (at > spanMs) {
      return count
    }

    count += 1
  }
}

// Parallel first dials (one per profile, before anything knows the host is down) may share t0; after that each
// probe waits at least the next backoff step.
function expectSpacedByBackoff(dials: number[]) {
  const probes = dials.filter(at => at !== dials[0])
  expect(dials.length - probes.length).toBeLessThanOrEqual(2)

  for (let i = 0; i < probes.length; i++) {
    expect(probes[i] - (i ? probes[i - 1] : dials[0])).toBeGreaterThanOrEqual(
      UNREACHABLE_BACKOFF_MS[Math.min(i, UNREACHABLE_BACKOFF_MS.length - 1)]
    )
  }
}

describe('renderer API requests against a down remote', () => {
  it('fail fast from the cached failure, dial at most once per backoff window, and reconnect once', async () => {
    const h = harness()
    const outcomes: any[] = []

    // Three minutes of renderer polling like the incident: two default-profile calls and one bb-dev call a second.
    for (let second = 0; second < 180; second++) {
      const results = await Promise.allSettled([
        h.ensure('mac-mini', 'default'),
        h.ensure('mac-mini', 'default'),
        h.ensure('mac-mini', 'bb-dev')
      ])

      for (const result of results) {
        outcomes.push(result.status === 'rejected' ? result.reason : 'ok')
      }

      h.advance(1_000)
    }

    expect(outcomes).toHaveLength(540)
    expect(outcomes.every(error => /timed out/.test(error.message) && error.kind === 'timeout')).toBe(true)
    expect(outcomes.filter(error => error.code === 'remote-unreachable-backoff').length).toBeGreaterThan(500)
    expect(h.remote.dials.length).toBeLessThanOrEqual(dialsAllowed(180_000) + 1) // + the parallel bb-dev first dial
    expect(h.remote.dials.length).toBeLessThan(10) // 540 requests, not 540 SSH dials
    expectSpacedByBackoff(h.remote.dials)
    expect(h.gate.reason(KEY)).toBe('unreachable')
    expect(h.logs.filter(line => /\[remote-dial-backoff\] \S+ unreachable \(timeout:/.test(line))).toHaveLength(1)

    // The host comes back: the first request after the window dials once, and the connection stays up.
    h.remote.down = false
    const dialsBefore = h.remote.dials.length
    h.advance(UNREACHABLE_BACKOFF_MS.at(-1)!)
    await expect(h.ensure('mac-mini', 'default')).resolves.toEqual({ baseUrl: 'http://127.0.0.1:50002' })

    for (let i = 0; i < 20; i++) {
      await expect(h.ensure('mac-mini', 'default')).resolves.toBeTruthy()
    }

    expect(h.remote.dials.length).toBe(dialsBefore + 1)
    expect(h.gate.paused(KEY)).toBe(false)
    expect(h.logs.filter(line => /reachable again; reconnected after/.test(line))).toHaveLength(1)

    // Other profiles of the host dial normally again.
    await expect(h.ensure('mac-mini', 'bb-dev')).resolves.toBeTruthy()
    expect(h.remote.dials.length).toBe(dialsBefore + 2)
  })

  it('the cached error keeps the dial failure readable by boot and names the next probe', async () => {
    const h = harness(() =>
      sshFailure(
        'auth-failed',
        'SSH authentication to daveey@192.0.2.20 failed. Original error: Permission denied (publickey).'
      )
    )

    await h.ensure('mac-mini', 'default').catch(() => {})

    const error: any = await h.ensure('mac-mini', 'default').catch(e => e)
    expect(error.code).toBe('remote-unreachable-backoff')
    expect(error.kind).toBe('auth-failed')
    expect(error.retryAt).toBe(h.now() + UNREACHABLE_BACKOFF_MS[0])
    // Boot still latches it as an SSH credential failure, not a retryable one (#72698 unchanged).
    expect(isSshAuthFailedBootFailure(error)).toBe(true)
    expect(isRetryableRemoteBootFailure({ attemptedRemote: true, isReauth: false, isSshAuthFailed: true })).toBe(false)
    expect(h.remote.dials).toHaveLength(1)
  })

  it('remembers only the failures the rule names; everything else dials per request as before', async () => {
    for (const kind of [
      'unreachable',
      'timeout',
      'transient-transport-error',
      'auth-failed',
      'interactive-auth',
      'host-key-changed'
    ]) {
      expect(classifyRemoteDialFailure({ kind })).toBe('unreachable')
    }

    expect(classifyRemoteDialFailure({ kind: 'update-in-progress' })).toBe('update')

    const passThrough = [
      sshClientFailedError('/usr/bin/ssh', new Error('ssh: command not found')),
      sshFailure('superseded', 'SSH operation was cancelled.'),
      sshFailure('spawn-failed', 'remote dashboard exited during startup'),
      sshFailure('ready-timeout', 'remote dashboard did not become ready'),
      sshFailure('ownership-changed', 'remote backend ownership changed'),
      sshFailure('remote-lockfile-skew', 'remote lockfile written by another version'),
      sshFailure('remote-profile-missing', "Profile 'bb-dev' does not exist on the remote."),
      sshFailure('hermes-not-found', 'hermes not found on the remote'),
      sshFailure('update-required', 'remote hermes is too old'),
      sshFailure('unknown', 'SSH error connecting to daveey@192.0.2.20: ssh process exited from signal SIGKILL'),
      new Error('something nobody tagged')
    ]

    for (const failure of passThrough) {
      expect(classifyRemoteDialFailure(failure)).toBe(null)

      const h = harness(() => failure)

      for (let i = 0; i < 5; i++) {
        await h.ensure('mac-mini', 'default').catch(() => {})
      }

      expect(h.remote.dials).toHaveLength(5)
      expect(h.gate.paused(KEY)).toBe(false)
    }

    // The terminal local ssh-client failure (#103288) reaches boot untouched.
    const clientFailure = sshClientFailedError('/usr/bin/ssh', new Error('ssh: command not found'))
    const h = harness(() => clientFailure)
    await expect(h.ensure('mac-mini', 'default')).rejects.toBe(clientFailure)
    expect(isSshClientFailedBootFailure(clientFailure)).toBe(true)
  })

  it('a probe that fails some other way ends the backoff instead of masking the new failure', async () => {
    const h = harness()
    await h.ensure('mac-mini', 'default').catch(() => {})
    h.advance(UNREACHABLE_BACKOFF_MS[0])
    h.remote.failure = () => sshFailure('spawn-failed', 'remote dashboard exited during startup')

    await expect(h.ensure('mac-mini', 'default')).rejects.toThrow(/exited during startup/)
    expect(h.gate.paused(KEY)).toBe(false)
  })

  it('a remote that goes down while paused for its own update switches to the unreachable schedule', async () => {
    const h = harness(() => {
      const error: any = new Error('Remote Hermes update process 1219818 is still running; SSH startup is paused.')
      error.kind = 'update-in-progress'

      return error
    })

    await h.ensure('mac-mini', 'default').catch(() => {})
    expect(h.gate.reason(KEY)).toBe('update')
    h.advance(2_000)
    h.remote.failure = timeout
    await expect(h.ensure('mac-mini', 'default')).rejects.toThrow(/timed out/)
    expect(h.gate.reason(KEY)).toBe('unreachable')

    const error: any = await h.ensure('mac-mini', 'default').catch(e => e)
    expect(error.code).toBe('remote-unreachable-backoff')
    expect(error.retryAt).toBe(h.now() + UNREACHABLE_BACKOFF_MS[0])
    expect(h.remote.dials).toHaveLength(2)
  })
})

describe('remote-prewarm against a down remote', () => {
  it('follows the gate window instead of its own redials, shares it with requests, and reconnects once', async () => {
    const h = harness()
    const prewarmLogs: string[] = []

    const prewarmer = new RemotePrewarmer({
      listTargets: () => [{ connectionId: 'mac-mini', profile: 'default' }],
      ensure: target => h.ensure(target.connectionId, target.profile),
      log: line => prewarmLogs.push(line),
      now: h.now,
      setTimer: () => ({})
    })

    await prewarmer.start() // first round hits the timeout
    expect(h.remote.dials).toHaveLength(1)
    expect(prewarmLogs.at(-1)).toMatch(/retry 1 in 2000 ms/)

    // Three minutes: prewarm ticks every 15 s and the renderer keeps asking every second in between.
    for (let second = 1; second <= 180; second++) {
      h.advance(1_000)
      await h.ensure('mac-mini', 'default').catch(() => {})

      if (second % 15 === 0) {
        await prewarmer.tick()
      }
    }

    expect(h.remote.dials.length).toBeLessThanOrEqual(dialsAllowed(181_000))
    expectSpacedByBackoff(h.remote.dials)
    expect(prewarmer.isWarm('mac-mini')).toBe(false)

    // No renderer traffic now: prewarm alone is the probe that sees the host come back.
    h.remote.down = false
    const dialsBefore = h.remote.dials.length

    for (let i = 0; i < 6 && !prewarmer.isWarm('mac-mini'); i++) {
      h.advance(15_000)
      await prewarmer.tick()
    }

    expect(prewarmer.isWarm('mac-mini')).toBe(true)
    expect(h.remote.dials.length).toBe(dialsBefore + 1)
    expect(prewarmLogs.filter(line => /mac-mini::default reconnected/.test(line))).toHaveLength(1)
    expect(h.logs.filter(line => /reachable again; reconnected after/.test(line))).toHaveLength(1)
    prewarmer.stop()
  })
})

describe('explicit user reconnect during a backoff', () => {
  it('Reload and retry (or a saved, applied or reachable-tested connection) dials right away', async () => {
    const h = harness()
    await h.ensure('mac-mini', 'default').catch(() => {})
    h.advance(UNREACHABLE_BACKOFF_MS[0])
    await h.ensure('mac-mini', 'default').catch(() => {}) // probe 2: next window is 5 s out
    expect(h.remote.dials).toHaveLength(2)

    // Still down: inside the window a request does not dial, but the user's retry does, and the schedule restarts.
    h.advance(1_000)
    await h.ensure('mac-mini', 'default').catch(() => {})
    expect(h.remote.dials).toHaveLength(2)
    h.gate.clear('reload and retry')
    await expect(h.ensure('mac-mini', 'default')).rejects.toThrow(/timed out/)
    expect(h.remote.dials).toHaveLength(3)
    expect((await h.ensure('mac-mini', 'default').catch(e => e)).retryAt).toBe(h.now() + UNREACHABLE_BACKOFF_MS[0])

    // Host back: the retry reconnects with exactly one dial, and requests then reuse it.
    h.remote.down = false
    h.advance(500)
    h.gate.clear('reload and retry')
    await expect(h.ensure('mac-mini', 'default')).resolves.toBeTruthy()
    await expect(h.ensure('mac-mini', 'default')).resolves.toBeTruthy()
    expect(h.remote.dials).toHaveLength(4)
    expect(
      h.logs.filter(line => /cleared .* for reload and retry; the next request dials now/.test(line))
    ).toHaveLength(2)
  })

  it('the registry Reconnect action and managed restores still bypass the gate and dial right away', async () => {
    const h = harness()
    await h.ensure('mac-mini', 'default').catch(() => {})
    expect(h.remote.dials).toHaveLength(1)

    h.remote.down = false
    h.advance(100) // well inside the 2 s window
    await expect(h.ensure('mac-mini', 'default', 'reconnect-correlation')).resolves.toBeTruthy()
    expect(h.remote.dials).toHaveLength(2)
    // The reconnected entry serves requests without another dial.
    await expect(h.ensure('mac-mini', 'default')).resolves.toBeTruthy()
    expect(h.remote.dials).toHaveLength(2)
  })
})

describe('main.ts wiring', () => {
  // The Electron main process cannot boot under vitest; pin the explicit-retry paths that clear the gate.
  const main = readFileSync(join(__dirname, 'main.ts'), 'utf8')

  const handler = (channel: string) => {
    const start = main.indexOf(`ipcMain.handle('${channel}'`)

    return main.slice(start, main.indexOf('\n})\n', start))
  }

  it('every explicit user retry clears the gate before it dials', () => {
    expect(handler('hermes:bootstrap:reset')).toMatch(/remoteUpdatePause\.clear\('reload and retry'\)/)
    expect(handler('hermes:bootstrap:repair')).toMatch(/remoteUpdatePause\.clear\('repair'\)/)
    expect(handler('hermes:connection-config:apply')).toMatch(/remoteUpdatePause\.clear\('connection applied'\)/)
    expect(handler('hermes:connections:save')).toMatch(/remoteUpdatePause\.clear\('connection saved'\)/)
    expect(handler('hermes:connections:test')).toMatch(
      /if \(result\?\.reachable\) \{\n\s+remoteUpdatePause\.clear\('reachable connection test'\)/
    )
  })
})
