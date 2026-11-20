import assert from 'node:assert/strict'

import { beforeEach, test, vi } from 'vitest'

// terminal-ipc.ts loads Electron and the native PTY at module scope; both are
// stubbed. The real ssh argv builder runs, so the remote command line is the
// one a gateway would receive.
const handles = vi.hoisted(() => ({ map: new Map<string, (...args: never[]) => unknown>() }))
const spawnMock = vi.hoisted(() => ({ fn: vi.fn() }))

vi.mock('electron', () => ({
  app: { getPath: () => '/tmp', getVersion: () => '0.0.0-test' },
  ipcMain: {
    handle: (channel: string, listener: (...args: never[]) => unknown) => {
      handles.map.set(channel, listener)
    }
  }
}))
vi.mock('node-pty', () => ({ default: { spawn: (...args: unknown[]) => spawnMock.fn(...args) } }))

import type { ConnectionRegistry } from './connection-registry'
import { terminalGatewayTarget } from './terminal-gateway-target'
import { registerTerminalIpc } from './terminal-ipc'
import { recoveryCliArgs, remoteRecoveryCommand, requestedRecovery } from './terminal-recovery'

const registry = {
  connections: [
    { id: 'local', kind: 'local', label: 'This device' },
    { id: 'nas', kind: 'ssh', label: 'NAS' },
    { id: 'winbox', kind: 'ssh', label: 'Windows box' },
    { id: 'cloudbox', kind: 'remote', label: 'Cloud box' }
  ],
  primary: 'local',
  version: 2
} as unknown as ConnectionRegistry

const ssh = { controlPath: '/tmp/ctl', host: 'nas.lan', user: 'hermes' }

const pool = new Map<string, Record<string, unknown>>([
  [
    'conn:nas::default',
    {
      hermesHome: '/home/hermes/.hermes',
      hermesPath: '/opt/hermes/bin/hermes',
      registryConnectionId: 'nas',
      remotePlatform: 'Linux',
      ssh
    }
  ],
  [
    'conn:winbox::default',
    { hermesPath: 'C:\\hermes\\hermes.exe', registryConnectionId: 'winbox', remotePlatform: 'Windows', ssh }
  ]
])

interface Started {
  connectionId?: string
  id: string
  recovery: true
  unavailable?: string
}

type Sender = ReturnType<typeof makeSender>
type Handler = (event: { sender: Sender }, ...args: unknown[]) => unknown

function makeSender(id: number) {
  return { id, isDestroyed: () => false, once: vi.fn(), send: vi.fn() }
}

function fakePty() {
  return { kill: vi.fn(), onData: vi.fn(), onExit: vi.fn(), pid: 4242, resize: vi.fn(), write: vi.fn() }
}

function register(overrides: Record<string, unknown> = {}) {
  const localHermesCli = vi.fn(async (args: string[]) => ({
    args: ['-m', 'hermes_cli.main', ...args],
    command: '/opt/py/bin/python',
    env: { HERMES_HOME: '/Users/me/.hermes' },
    shell: false
  }))

  registerTerminalIpc({
    activeSshTerminalTarget: () => ({ scope: 'conn:nas::default', ssh }),
    ensureBackend: async () => undefined,
    ensureGatewayBackend: async () => undefined,
    findOnPath: () => null,
    gatewayTarget: owner => terminalGatewayTarget(owner, registry, pool),
    getSshConnectionState: scope => pool.get(scope),
    isWindows: false,
    localHermesCli,
    rememberLog: () => {},
    sshBinary: () => '/usr/bin/ssh',
    ...overrides
  } as Parameters<typeof registerTerminalIpc>[0])

  const call = (channel: string) => handles.map.get(channel) as unknown as Handler

  return {
    localHermesCli,
    start: (sender: Sender, payload: unknown) =>
      call('hermes:terminal:recovery:start')({ sender }, payload) as Promise<Started>,
    call
  }
}

const request = { connectionId: 'local', credentialId: 'a1b2c3', provider: 'anthropic' }

beforeEach(() => {
  handles.map.clear()
  spawnMock.fn.mockReset()
  spawnMock.fn.mockImplementation(() => fakePty())
})

test('a recovery request names an allowed provider and a plain row id, or nothing', () => {
  assert.deepEqual(requestedRecovery({ credentialId: 'a1b2c3', provider: 'anthropic' }), {
    credentialId: 'a1b2c3',
    provider: 'anthropic'
  })

  for (const bad of [
    null,
    {},
    { credentialId: 'a1b2c3' },
    { credentialId: 'a1b2c3', provider: 'openai-codex' },
    { credentialId: 'a1b2c3', provider: 'anthropic; rm -rf ~' },
    { credentialId: '', provider: 'anthropic' },
    { credentialId: '-rf', provider: 'anthropic' },
    { credentialId: 'a b', provider: 'anthropic' },
    { credentialId: "a';id;'", provider: 'anthropic' },
    { credentialId: 'a$(id)', provider: 'anthropic' },
    { credentialId: 'a\nb', provider: 'anthropic' },
    { credentialId: 'x'.repeat(129), provider: 'anthropic' },
    { credentialId: 7, provider: 'anthropic' }
  ]) {
    assert.equal(requestedRecovery(bad), null, JSON.stringify(bad))
  }
})

test('recovery arguments are the fixed reauth command, with a plain profile only', () => {
  const recovery = { credentialId: 'a1b2c3', provider: 'anthropic' }

  assert.deepEqual(recoveryCliArgs(recovery), ['auth', 'reauth', 'anthropic', 'a1b2c3'])
  assert.deepEqual(recoveryCliArgs(recovery, 'work'), ['--profile', 'work', 'auth', 'reauth', 'anthropic', 'a1b2c3'])
  assert.equal(recoveryCliArgs(recovery, '--help'), null)
  assert.equal(recoveryCliArgs(recovery, 'a b'), null)
  assert.equal(recoveryCliArgs(recovery, "w';id;'"), null)
})

test('the remote command replaces the shell with the quoted launcher and arguments', () => {
  const args = ['auth', 'reauth', 'anthropic', 'a1b2c3']

  assert.equal(
    remoteRecoveryCommand({ hermesHome: '/home/h/.hermes', hermesPath: "/opt/it's/hermes" }, args),
    "HERMES_HOME='/home/h/.hermes' exec '/opt/it'\\''s/hermes' 'auth' 'reauth' 'anthropic' 'a1b2c3'"
  )
  assert.equal(
    remoteRecoveryCommand({ hermesPath: '/opt/hermes' }, args),
    "exec '/opt/hermes' 'auth' 'reauth' 'anthropic' 'a1b2c3'"
  )
  assert.equal(remoteRecoveryCommand({ hermesPath: '' }, args), null)
  assert.equal(remoteRecoveryCommand({ hermesPath: '/opt/hermes\nid' }, args), null)
  assert.equal(remoteRecoveryCommand({ hermesHome: '/h\nid', hermesPath: '/opt/hermes' }, args), null)
})

test('a local recovery runs only the resolved launcher with the reauth arguments', async () => {
  const { localHermesCli, start } = register()
  const started = await start(makeSender(1), { ...request, cols: 100, profile: 'work', rows: 30 })

  assert.equal(started.unavailable, undefined)
  assert.equal(started.recovery, true)
  assert.equal(started.connectionId, 'local')
  assert.ok(started.id)
  assert.deepEqual(localHermesCli.mock.calls, [[['--profile', 'work', 'auth', 'reauth', 'anthropic', 'a1b2c3']]])
  assert.equal(spawnMock.fn.mock.calls.length, 1)

  const [command, args, options] = spawnMock.fn.mock.calls[0]

  assert.equal(command, '/opt/py/bin/python')
  assert.deepEqual(args, ['-m', 'hermes_cli.main', '--profile', 'work', 'auth', 'reauth', 'anthropic', 'a1b2c3'])
  assert.equal(options.cols, 100)
  assert.equal(options.rows, 30)
  assert.equal(options.env.HERMES_HOME, '/Users/me/.hermes')
})

test('an SSH recovery runs the gateway launcher over that gateway tunnel, never a shell', async () => {
  const { localHermesCli, start } = register()
  const started = await start(makeSender(1), { ...request, connectionId: 'nas' })

  assert.equal(started.unavailable, undefined)
  assert.equal(started.connectionId, 'nas')
  assert.equal(localHermesCli.mock.calls.length, 0)

  const [command, args] = spawnMock.fn.mock.calls[0]

  assert.equal(command, '/usr/bin/ssh')
  assert.equal(
    args.at(-1),
    "HERMES_HOME='/home/hermes/.hermes' exec '/opt/hermes/bin/hermes' 'auth' 'reauth' 'anthropic' 'a1b2c3'"
  )
  assert.equal(args.at(-2), 'hermes@nas.lan')
  assert.ok(!args.some((arg: string) => arg.includes('$SHELL')))
})

test('a request that fails any check starts no process', async () => {
  const { localHermesCli, start } = register()
  const sender = makeSender(1)

  for (const payload of [
    undefined,
    { credentialId: 'a1b2c3', provider: 'anthropic' },
    { ...request, connectionId: '  ' },
    { ...request, provider: 'openai-codex' },
    { ...request, credentialId: 'a;id' },
    { ...request, profile: 'w;id' },
    { ...request, command: 'id', credentialId: undefined }
  ]) {
    assert.deepEqual(await start(sender, payload), { id: '', recovery: true, unavailable: 'invalid' })
  }

  assert.equal(spawnMock.fn.mock.calls.length, 0)
  assert.equal(localHermesCli.mock.calls.length, 0)
})

test('a gateway without a shell, a removed gateway and a Windows gateway are reported, not run here', async () => {
  const { localHermesCli, start } = register()
  const sender = makeSender(1)

  assert.equal((await start(sender, { ...request, connectionId: 'cloudbox' })).unavailable, 'no-shell')
  assert.equal((await start(sender, { ...request, connectionId: 'gone' })).unavailable, 'missing')
  assert.equal((await start(sender, { ...request, connectionId: 'winbox' })).unavailable, 'unsupported')
  assert.equal(spawnMock.fn.mock.calls.length, 0)
  assert.equal(localHermesCli.mock.calls.length, 0)
})

test('an SSH gateway whose launcher path is unknown is reported', async () => {
  const bare = new Map([['conn:nas::default', { registryConnectionId: 'nas', remotePlatform: 'Linux', ssh }]])

  const { start } = register({
    gatewayTarget: (owner: { connectionId: string }) => terminalGatewayTarget(owner, registry, bare),
    getSshConnectionState: (scope: string) => bare.get(scope)
  })

  assert.equal((await start(makeSender(1), { ...request, connectionId: 'nas' })).unavailable, 'unsupported')
  assert.equal(spawnMock.fn.mock.calls.length, 0)
})

test('no local runtime, a Windows Desktop, or an older host wiring start nothing', async () => {
  const sender = makeSender(1)

  assert.equal((await register({ localHermesCli: async () => null }).start(sender, request)).unavailable, 'no-runtime')
  assert.equal((await register({ localHermesCli: undefined }).start(sender, request)).unavailable, 'no-runtime')
  assert.equal(
    (
      await register({
        localHermesCli: async () => ({ args: [], command: 'hermes.cmd', env: {}, shell: true })
      }).start(sender, request)
    ).unavailable,
    'no-runtime'
  )
  assert.equal((await register({ isWindows: true }).start(sender, request)).unavailable, 'unsupported')
  assert.equal(spawnMock.fn.mock.calls.length, 0)
})

test('only the window that started a recovery can type into it, size it, read it or end it', async () => {
  const pty = fakePty()

  spawnMock.fn.mockImplementation(() => pty)

  const { call, start } = register()
  const owner = makeSender(1)
  const other = makeSender(2)
  const { id } = await start(owner, request)

  assert.equal(await call('hermes:terminal:write')({ sender: other }, id, 'x'), false)
  assert.equal(await call('hermes:terminal:resize')({ sender: other }, id, { cols: 10, rows: 10 }), false)
  assert.equal(await call('hermes:terminal:attach')({ sender: other }, id), false)
  assert.equal(await call('hermes:terminal:dispose')({ sender: other }, id), false)
  assert.equal(pty.write.mock.calls.length, 0)
  assert.equal(pty.resize.mock.calls.length, 0)
  assert.equal(pty.kill.mock.calls.length, 0)

  assert.equal(await call('hermes:terminal:attach')({ sender: owner }, id), true)
  assert.equal(await call('hermes:terminal:write')({ sender: owner }, id, 'replace\r'), true)
  assert.deepEqual(pty.write.mock.calls, [['replace\r']])
  assert.equal(await call('hermes:terminal:cwd')({ sender: owner }, id), null)
  assert.equal(await call('hermes:terminal:dispose')({ sender: owner }, id), true)
  assert.equal(pty.kill.mock.calls.length, 1)
  assert.equal(await call('hermes:terminal:write')({ sender: owner }, id, 'x'), false)
})

test('recovery output reaches only the window that started it', async () => {
  const pty = fakePty()

  spawnMock.fn.mockImplementation(() => pty)

  const { call, start } = register()
  const owner = makeSender(1)
  const { id } = await start(owner, request)

  await call('hermes:terminal:attach')({ sender: owner }, id)

  const onData = pty.onData.mock.calls[0][0] as (data: string) => void

  onData('Open this URL')
  await new Promise(resolve => setTimeout(resolve, 30))

  const sent = owner.send.mock.calls.filter(([channel]) => channel === `hermes:terminal:${id}:data`)

  assert.ok(sent.length >= 1)
})

test('an ordinary shell tab keeps its existing write path', async () => {
  const pty = fakePty()

  spawnMock.fn.mockImplementation(() => pty)

  const { call } = register()
  const owner = makeSender(1)

  const session = (await call('hermes:terminal:start')({ sender: owner }, { connectionId: 'local' })) as { id: string }

  assert.equal(await call('hermes:terminal:write')({ sender: makeSender(2) }, session.id, 'ls\r'), true)
  assert.deepEqual(pty.write.mock.calls, [['ls\r']])
})
