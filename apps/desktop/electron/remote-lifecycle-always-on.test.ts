import assert from 'node:assert/strict'

import { test } from 'vitest'

import { connect, readAlwaysOnState } from './remote-lifecycle'

// Part B (instant session loads): a launchd/systemd-supervised always-on `serve` per remote host
// (hermes_cli/always_on_dashboard.py). connect() reuses it for the default profile before the owned-backend
// lockfile path, waits out a supervisor restart (core update, crash) instead of spawning beside it, and never
// kills it or writes a lockfile for it.

const OWNERSHIP_ID = '0123456789abcdef0123456789abcdef'
const TOKEN = 'a'.repeat(64)
const NONCE = 'fedcba9876543210'

function record(over: any = {}) {
  return { schemaVersion: 1, pid: 4242, port: 9147, spawnNonce: NONCE, hermesHome: '~/.hermes', ...over }
}

// `states` is what successive always-on reads return: 'NONE' (not installed) or [ALIVE|DEAD, record].
function fakeHost(states: any[], extra: any[] = []) {
  const calls: string[] = []
  let read = 0

  return {
    calls,
    async exec(cmd: string) {
      calls.push(cmd)

      if (cmd.includes('desktop-ssh/always-on')) {
        const state = states[Math.min(read++, states.length - 1)]

        return state === 'NONE' ? 'NONE\n' : `${state[0]}\n${JSON.stringify(state[1])}\n${TOKEN}\n`
      }

      if (cmd.includes('.hermes-update-in-progress') && !/setsid|nohup/.test(cmd)) {
        return 'CLEAR'
      }

      for (const [matcher, resp] of [
        [/uname/, 'Darwin\narm64'],
        [/\[ -x/, 'OK'],
        [/kill -0/, 'ALIVE'],
        ...extra
      ] as any[]) {
        if (matcher.test(cmd)) {
          return typeof resp === 'function' ? resp(cmd) : resp
        }
      }

      return ''
    }
  }
}

function deps(ssh, over: any = {}) {
  const forwards: number[] = []
  const cancelled: number[] = []

  return {
    forwards,
    cancelled,
    ssh,
    ownershipId: OWNERSHIP_ID,
    profile: '',
    forward: async (_local, remote) => void forwards.push(remote),
    cancelForward: async (_local, remote) => void cancelled.push(remote),
    pickLocalPort: async () => 50001,
    waitForHermes: async () => {},
    probeReuseProof: async (_url, token, nonce) =>
      token === TOKEN && nonce === NONCE ? 'authenticated-ok' : 'authenticated-stale',
    adoptServedToken: async (_baseUrl, token) => token,
    rememberLog: () => {},
    readyTimeoutMs: 2000,
    sleep: async () => {},
    ...over
  }
}

const SPAWNS = /setsid|nohup/
const WRITES_LOCK = /backend\.lock\.json/
const KILLS = /\bkill (-9 |-TERM )?4242\b|os\.kill\(/

test('connect() reuses the always-on dashboard: no spawn, no lockfile, nothing killed', async () => {
  const ssh = fakeHost([['ALIVE', record()]])
  const d = deps(ssh)
  const result: any = await connect(d)

  assert.equal('alwaysOn' in result && result.alwaysOn, true)
  assert.equal(result.reused, true)
  assert.equal(result.pid, 4242)
  assert.equal(result.remotePort, 9147)
  assert.equal(result.token, TOKEN)
  assert.deepEqual(d.forwards, [9147])
  assert.ok(!ssh.calls.some(c => SPAWNS.test(c)), 'must not spawn an owned backend')
  assert.ok(!ssh.calls.some(c => WRITES_LOCK.test(c)), 'must not read or write the owned lockfile')
  assert.ok(!ssh.calls.some(c => KILLS.test(c)))
})

test('after a core update, connect() waits for the supervisor restart and reuses the restarted dashboard', async () => {
  const logs: string[] = []
  // 1: the old pid still runs the replaced runtime; 2: the code-skew watchdog retired it; 3: launchd/systemd restarted it.
  const ssh = fakeHost([
    ['ALIVE', record()],
    ['DEAD', record()],
    ['ALIVE', record({ pid: 5151 })]
  ])
  let probes = 0

  const d = deps(ssh, {
    rememberLog: line => logs.push(line),
    probeReuseProof: async () => (++probes === 1 ? 'authenticated-stale' : 'authenticated-ok')
  })

  const result: any = await connect(d)

  assert.equal('alwaysOn' in result && result.alwaysOn, true)
  assert.equal(result.pid, 5151)
  assert.ok(!ssh.calls.some(c => SPAWNS.test(c)), 'a restart window must not become a spawn')
  assert.deepEqual(d.cancelled, [9147], 'the stale probe forward is closed')
  assert.ok(
    logs.some(l => /stale after an update\); waiting for its supervisor restart/.test(l)),
    logs.join('\n')
  )
  assert.ok(
    logs.some(l => /reusing always-on remote dashboard pid=5151/.test(l)),
    logs.join('\n')
  )
})

test('a dashboard that is still down after the restart wait falls back to the owned-backend path', async () => {
  const logs: string[] = []
  let clock = 0

  const ssh = fakeHost(
    [['DEAD', record()]],
    [
      [/grep -q ssh-session-token-file/, 'YES\n'],
      [/setsid/, '777\n'],
      [/cat .*\.log/, 'HERMES_DASHBOARD_READY port=51999\n']
    ]
  )

  const d = deps(ssh, {
    rememberLog: line => logs.push(line),
    now: () => clock,
    sleep: async ms => void (clock += ms),
    alwaysOnRestartWaitMs: 3000
  })

  try {
    await connect(d)
  } catch {
    // The owned spawn may fail in this double; only that it was attempted matters.
  }

  assert.ok(
    logs.some(l => /always-on dashboard unavailable \(pid 4242 not running\)/.test(l)),
    logs.join('\n')
  )
  assert.ok(
    ssh.calls.some(c => SPAWNS.test(c)),
    'falls back to spawning an owned backend'
  )
})

test('no always-on dashboard installed: connect() goes straight to the lockfile path', async () => {
  const ssh = fakeHost(['NONE'])
  const d = deps(ssh)

  try {
    await connect(d)
  } catch {
    void 0
  }

  assert.equal(ssh.calls.filter(c => c.includes('desktop-ssh/always-on')).length, 1, 'read once, no waiting')
  assert.ok(ssh.calls.some(c => WRITES_LOCK.test(c)))
})

test('a named profile keeps its own owned backend and never reads the always-on record', async () => {
  const ssh = fakeHost([['ALIVE', record()]])

  try {
    await connect(deps(ssh, { profile: 'work' }))
  } catch {
    void 0
  }

  assert.ok(!ssh.calls.some(c => c.includes('desktop-ssh/always-on')))
})

test('readAlwaysOnState rejects a malformed record or token', async () => {
  const bad = { exec: async () => `ALIVE\n${JSON.stringify(record({ spawnNonce: 'nope' }))}\n${TOKEN}\n` }
  assert.deepEqual(await readAlwaysOnState(bad), { installed: true, alive: false, record: null, token: '' })

  const ok = { exec: async () => `ALIVE\n${JSON.stringify(record())}\n${TOKEN}\n` }
  assert.equal((await readAlwaysOnState(ok))?.alive, true)
})
