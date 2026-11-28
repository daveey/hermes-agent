import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  connect,
  fingerprintToken,
  LOCKFILE_SCHEMA_VERSION,
  PROTOCOL_VERSION,
  release,
  spawnLogPath
} from './remote-lifecycle'
import { remoteTeardownAction } from './ssh-teardown-policy'

// W0 (warm dashboards): a normal disconnect releases the owned remote backend instead of killing it, so the next
// connect (after a quit, sleep or reconnect) reuses it. The backend's own idle watchdog
// (hermes_cli/web_server_idle_exit.py, 900 s after the last client) bounds how long a released backend lives.

const OWNERSHIP_ID = '0123456789abcdef0123456789abcdef'
const SPAWN_NONCE = '0123456789abcdef'

function ownedLock(over: any = {}) {
  return {
    schemaVersion: LOCKFILE_SCHEMA_VERSION,
    protocolVersion: PROTOCOL_VERSION,
    ownershipId: OWNERSHIP_ID,
    spawnNonce: SPAWN_NONCE,
    pid: 333,
    port: 40000,
    profile: '',
    hermesPath: '~/.local/bin/hermes',
    hermesHome: '~/.hermes',
    logPath: spawnLogPath(OWNERSHIP_ID, SPAWN_NONCE),
    tokenFingerprint: fingerprintToken('stored-token'),
    startedAt: '2026-07-14T00:00:00.000Z',
    creationTime: 'linux:123456',
    ...over
  }
}

// A remote whose lockfile survives between calls unless something removes it, like a real host.
function fakeRemote(lock: any, extra: any[] = []) {
  const calls: string[] = []
  let lockJson = lock ? JSON.stringify(lock) : ''

  const rules: any[] = [
    [/rm -f .*backend\.lock\.json/, () => ((lockJson = ''), '')],
    [/cat .*backend\.lock\.json/, () => lockJson],
    [/uname/, 'Linux\nx86_64'],
    [/\[ -x/, 'OK'],
    [/kill -0/, 'ALIVE'],
    [/print\("OWNED"/, 'OWNED\n'],
    ...extra
  ]

  return {
    calls,
    hasLock: () => Boolean(lockJson),
    async exec(cmd: string) {
      calls.push(cmd)

      if (cmd.includes('.hermes-update-in-progress') && !/setsid|nohup/.test(cmd)) {
        return 'CLEAR'
      }

      for (const [matcher, resp] of rules) {
        if (matcher.test(cmd)) {
          return typeof resp === 'function' ? resp(cmd) : resp
        }
      }

      return ''
    }
  }
}

function connectDeps(ssh, over: any = {}) {
  return {
    ssh,
    ownershipId: OWNERSHIP_ID,
    profile: '',
    forward: async () => {},
    cancelForward: async () => {},
    pickLocalPort: async () => 50001,
    waitForHermes: async () => {},
    probeReuseProof: async () => 'authenticated-ok',
    adoptServedToken: async (_baseUrl, token) => token,
    rememberLog: () => {},
    readyTimeoutMs: 2000,
    ...over
  }
}

const KILLS = /\bkill (-9 )?333\b|os\.kill\(/

test('release() keeps an owned backend alive and its lockfile in place', async () => {
  const ssh = fakeRemote(ownedLock())
  const logs: string[] = []

  await release(ssh, OWNERSHIP_ID, line => logs.push(line))

  assert.ok(!ssh.calls.some(command => KILLS.test(command)), 'release must not terminate the backend')
  assert.ok(ssh.hasLock(), 'release must keep the lockfile so the next connect can reuse the backend')
  assert.ok(
    logs.some(line => /released remote dashboard pid=333/.test(line)),
    logs.join('\n')
  )
})

test('release() fails closed on a foreign or corrupt lockfile: no kill, no removal', async () => {
  const ssh = fakeRemote({ schemaVersion: 999, garbage: true })

  await release(ssh, OWNERSHIP_ID, () => {})

  assert.ok(!ssh.calls.some(command => KILLS.test(command)))
  assert.ok(ssh.hasLock())
})

test('after release, a reconnect with the persisted token reuses the backend instead of spawning', async () => {
  const ssh = fakeRemote(ownedLock())
  await release(ssh, OWNERSHIP_ID, () => {})

  // A restarted Desktop passes the token it persisted (encrypted) when it first adopted this backend.
  const result = await connect(connectDeps(ssh, { reuseToken: 'stored-token' }))

  assert.equal(result.reused, true)
  assert.equal(result.pid, 333)
  assert.ok(!ssh.calls.some(command => /setsid/.test(command)), 'reuse must not spawn a new dashboard')
})

test('connect() logs the exact reason a present backend was not reused', async () => {
  const cases: [string, any, any, RegExp][] = [
    ['no stored token', ownedLock(), {}, /reuse miss: .*no stored token/],
    ['token mismatch', ownedLock(), { reuseToken: 'other-token' }, /reuse miss: .*token fingerprint mismatch/],
    ['profile', ownedLock({ profile: 'work' }), { reuseToken: 'stored-token' }, /reuse miss: .*profile work != ''/],
    [
      'hermes path',
      ownedLock({ hermesPath: '/old/bin/hermes' }),
      { reuseToken: 'stored-token' },
      /reuse miss: .*hermes path/
    ]
  ]

  for (const [label, lock, over, expected] of cases) {
    const logs: string[] = []

    const ssh = fakeRemote(lock, [
      [/grep -q ssh-session-token-file/, 'YES\n'],
      [/setsid/, '777\n'],
      [/cat .*\.log/, 'HERMES_DASHBOARD_READY port=51999\n']
    ])

    try {
      await connect(connectDeps(ssh, { rememberLog: line => logs.push(line), ...over }))
    } catch {
      // Spawning a replacement may fail in this double; only the logged reason matters here.
    }

    assert.ok(
      logs.some(line => expected.test(line)),
      `${label}: ${logs.join('\n')}`
    )
  }
})

test('teardown policy: normal disconnects release, deliberate ones terminate, unknown reasons terminate', () => {
  for (const reason of ['quit', 'bootstrap-retry', 'revalidate', 'resume', 'idle-stop'] as const) {
    assert.equal(remoteTeardownAction(reason, 'Linux'), 'release', reason)
    assert.equal(remoteTeardownAction(reason, 'Darwin'), 'release', reason)
  }

  for (const reason of [
    'fingerprint-change',
    'connection-removed',
    'recycle',
    'config-apply',
    'profile-teardown'
  ] as const) {
    assert.equal(remoteTeardownAction(reason, 'Linux'), 'terminate', reason)
  }

  assert.equal(remoteTeardownAction('something-new' as any, 'Linux'), 'terminate')
  assert.equal(remoteTeardownAction('quit', 'Windows'), 'skip-windows')
})

test('connect() logs a reason when the backend is alive and ours but its runtime was replaced', async () => {
  const logs: string[] = []

  const ssh = fakeRemote(ownedLock(), [
    [/grep -q ssh-session-token-file/, 'YES\n'],
    [/setsid/, '777\n'],
    [/cat .*\.log/, 'HERMES_DASHBOARD_READY port=51999\n']
  ])

  try {
    await connect(
      connectDeps(ssh, {
        reuseToken: 'stored-token',
        probeReuseProof: async () => 'authenticated-stale',
        rememberLog: line => logs.push(line)
      })
    )
  } catch {
    // Replacing the stale backend may fail in this double; only the logged reason matters here.
  }

  assert.ok(
    logs.some(line => /reuse miss: pid 333 runs a replaced runtime/.test(line)),
    logs.join('\n')
  )
})
