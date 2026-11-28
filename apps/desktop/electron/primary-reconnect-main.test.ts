import fs from 'node:fs'

import ts from 'typescript'
import { describe, expect, it, vi } from 'vitest'

import { resolveProfileBackendRoute } from './connection-config'
import { desktopConnectionRequestInheritsPrimary, resolveDesktopConnectionRequest } from './desktop-profile'
import { joinPrimaryBoot, primaryBootRequest, primaryDialClaimKey } from './primary-boot-join'

// Execute the production IPC and backend-selection declarations, isolating
// Electron startup/process spawning, not the route decision being tested.
const main = ts.createSourceFile(
  'main.ts',
  fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8'),
  ts.ScriptTarget.Latest,
  true
)
const preload = ts.createSourceFile(
  'preload.ts',
  fs.readFileSync(new URL('./preload.ts', import.meta.url), 'utf8'),
  ts.ScriptTarget.Latest,
  true
)

function declaration(name: string) {
  const node = main.statements.find(statement => ts.isFunctionDeclaration(statement) && statement.name?.text === name)

  if (!node) {
    throw new Error(`Missing production function ${name}`)
  }

  return node.getText(main)
}

function bridge(name: string) {
  let found = ''

  const visit = (node: ts.Node) => {
    if (ts.isPropertyAssignment(node) && node.name.getText(preload) === name) {
      found = node.initializer.getText(preload)
    }
    ts.forEachChild(node, visit)
  }

  visit(preload)

  if (!found) {
    throw new Error(`Missing preload bridge ${name}`)
  }

  return found
}

function harness(localGateway = false) {
  const primary = {
    baseUrl: 'https://hub.invalid',
    wsUrl: 'wss://hub.invalid/api/ws',
    authMode: 'oauth',
    headers: { Authorization: 'fixture' },
    mode: 'remote'
  }
  const local = {
    baseUrl: 'http://local.invalid',
    wsUrl: 'ws://local.invalid/api/ws',
    headers: {},
    mode: 'local',
    ...(localGateway ? { gatewayEndpoint: {} } : {})
  }
  const handlers = new Map<string, (...args: any[]) => any>()
  const start = vi.fn(async () => ({ ...primary, ...(localGateway ? { gatewayEndpoint: {} } : {}) }))
  const pool = vi.fn(async () => local)
  const mint = vi.fn(async () => 'one-use-fixture')
  const registry = vi.fn(async () => ({ ...local, profile: 'coding' }))
  const forget = vi.fn()

  const dial = vi.fn(async (opts: { profile: string; forget: () => unknown; ensure: () => Promise<unknown> }) => {
    await opts.forget()
    await opts.ensure()

    return opts.profile
  })

  const deps = {
    primaryBootRequest,
    joinPrimaryBoot,
    primaryDialClaimKey,
    resolveProfileBackendRoute,
    resolveDesktopConnectionRequest,
    desktopConnectionRequestInheritsPrimary,
    primaryProfileKey: () => 'remote-primary',
    // The old descriptor's default label now names a stored local pool. The
    // primary has been re-pinned (e.g. a remote roster repair during boot).
    profileRouteOptions: (key: string) => ({
      primaryProfile: 'remote-primary',
      primaryRemoteActive: true,
      ownEntry: key === 'default',
      profileRemoteOverride: key === 'other-remote'
    }),
    profileDeletionGate: { assertCanStart: vi.fn() },
    startHermes: start,
    dialPoolBackend: pool,
    backendPool: new Map(),
    setWslBridgeProfileState: vi.fn(),
    assertNotPassiveSpawn: vi.fn(),
    newPoolEntry: () => ({}),
    logPoolSpawnFailure: vi.fn(),
    forgetFailedPoolEntry: vi.fn(),
    mintGatewayWsTicket: mint,
    localGatewayDial: dial,
    forgetLocalGatewayDescriptor: forget,
    HERMES_HOME: '/fixture/home',
    localGatewayDials: { prepare: vi.fn() },
    buildGatewayWsUrlWithTicket: (base: string, ticket: string) =>
      `${base.replace('https:', 'wss:')}/api/ws?ticket=${ticket}`,
    rememberRemoteWsHeaders: vi.fn(),
    windowConnectionRoutes: new Map([[7, { connectionId: 'foreground', profile: 'coding', registryScoped: true }]]),
    backendDialClaimKey: (id: string, profile: string) => `${id}:${profile}`,
    backendDialClaims: { run: (_key: string, run: () => unknown) => run() },
    ensureRegistryBackend: registry,
    liveWindowState: () => ({}),
    BrowserWindow: { fromWebContents: vi.fn() },
    getWindowState: vi.fn(),
    mainWindow: null,
    resolvedConnectionId: () => null,
    readDesktopConnectionsRegistry: () => ({}),
    overlayWindowState: (connection: unknown) => connection,
    captureStartupDeadline: (promise: unknown) => promise,
    gatewayWsUrlIpcResult: (run: () => unknown) => run(),
    ipcMain: { handle: (name: string, handler: (...args: any[]) => any) => handlers.set(name, handler) },
    ipcRenderer: { invoke: (name: string, ...args: any[]) => handlers.get(name)!({ sender: { id: 7 } }, ...args) },
    unwrapConnectionDeadline: (value: unknown) => value
  }

  const ipc = main.statements
    .filter(
      statement =>
        ts.isExpressionStatement(statement) &&
        ts.isCallExpression(statement.expression) &&
        statement.expression.expression.getText(main) === 'ipcMain.handle' &&
        ['hermes:connection', 'hermes:gateway:ws-url'].includes(
          (statement.expression.arguments[0] as ts.StringLiteral).text
        )
    )
    .map(statement => statement.getText(main))
  expect(ipc).toHaveLength(2)

  const source = [
    declaration('ensureBackend'),
    declaration('freshGatewayWsUrl'),
    declaration('connectDesktopProfileRoute'),
    ...ipc,
    `return { getConnection: ${bridge('getConnection')}, getGatewayWsUrl: ${bridge('getGatewayWsUrl')} }`
  ].join('\n')

  const compiled = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None }
  }).outputText
  const api = new Function(...Object.keys(deps), compiled)(...Object.values(deps))

  return { api, start, pool, mint, registry, primary, local, forget, dial }
}

describe('production main/preload primary reconnect routing', () => {
  it('lets an inherited profile-less reconnect bypass the foreground registry route at the real IPC handler', async () => {
    const h = harness()
    const connection = await h.api.getConnection(undefined, { inheritsPrimary: true })
    expect(connection.baseUrl).toBe(h.primary.baseUrl)
    expect(connection.profile).toBe('remote-primary')
    expect(h.registry).not.toHaveBeenCalled()
  })

  it('does not interpret a truthy non-boolean flag as primary inheritance on a profile-less request', async () => {
    const h = harness()
    const connection = await h.api.getConnection(undefined, { inheritsPrimary: 'true' })
    expect(connection.connectionId).toBe('foreground')
    expect(h.registry).toHaveBeenCalledWith('foreground', 'coding')
    expect(h.start).not.toHaveBeenCalled()
  })

  it('uses the inherited primary label for local ticket routing and stale descriptor recovery', async () => {
    const h = harness(true)
    expect(await h.api.getGatewayWsUrl('default', { inheritsPrimary: true })).toBe('remote-primary')
    expect(h.dial).toHaveBeenCalledWith(expect.objectContaining({ profile: 'remote-primary' }))
    expect(h.forget).toHaveBeenCalledWith('remote-primary')
    expect(h.start).toHaveBeenCalledWith('remote-primary', true)
    expect(h.pool).not.toHaveBeenCalled()
  })

  it('keeps a named local gateway ticket and recovery scoped to the named caller', async () => {
    const h = harness(true)
    expect(await h.api.getGatewayWsUrl('default')).toBe('default')
    expect(h.forget).toHaveBeenCalledWith('default')
    expect(h.start).not.toHaveBeenCalled()
  })

  it('reconnects the inherited descriptor to the pinned primary, not the same-named local pool or foreground', async () => {
    const h = harness()
    const connection = await h.api.getConnection('default', { inheritsPrimary: true })
    expect(connection.baseUrl).toBe(h.primary.baseUrl)
    expect(connection.profile).toBe('remote-primary')
    expect(connection.inheritsPrimary).toBe(true)
    expect(h.start).toHaveBeenCalledWith('remote-primary', true)
    expect(h.pool).not.toHaveBeenCalled()
    expect(h.registry).not.toHaveBeenCalled()
  })

  it('mints a fresh ticket on that same primary through the actual preload and ws-url IPC', async () => {
    const h = harness()
    expect(await h.api.getGatewayWsUrl('default', { inheritsPrimary: true })).toBe(
      'wss://hub.invalid/api/ws?ticket=one-use-fixture'
    )
    expect(h.mint).toHaveBeenCalledWith(h.primary.baseUrl, h.primary.headers)
    expect(h.start).toHaveBeenCalledWith('remote-primary', true)
    expect(h.pool).not.toHaveBeenCalled()
  })

  it('does not mark a named request served by the primary as inherited', async () => {
    const h = harness()
    const connection = await h.api.getConnection('served')
    expect(connection.baseUrl).toBe(h.primary.baseUrl)
    expect(connection.profile).toBe('served')
    expect(connection.sharedPrimary).toBe(true)
    expect(connection.inheritsPrimary).toBe(false)
    expect(h.start).toHaveBeenCalledWith('served', false)
    expect(h.pool).not.toHaveBeenCalled()
    expect(h.registry).not.toHaveBeenCalled()
  })

  it('keeps explicit named pools and profile-less foreground registry routing unchanged', async () => {
    for (const opts of [undefined, { inheritsPrimary: false }, { inheritsPrimary: 'true' }]) {
      const h = harness()
      expect((await h.api.getConnection('default', opts)).baseUrl).toBe(h.local.baseUrl)
      expect(await h.api.getGatewayWsUrl('default', opts)).toBe(h.local.wsUrl)
      expect(h.start).not.toHaveBeenCalled()
      expect(h.mint).not.toHaveBeenCalled()
    }

    const h = harness()
    expect((await h.api.getConnection('other-remote')).baseUrl).toBe(h.local.baseUrl)
    expect((await h.api.getConnection()).connectionId).toBe('foreground')
    expect(h.registry).toHaveBeenCalledWith('foreground', 'coding')
  })
})
