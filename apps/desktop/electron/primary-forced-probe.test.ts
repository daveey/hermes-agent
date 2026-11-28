import fs from 'node:fs'

import ts from 'typescript'
import { expect, test, vi } from 'vitest'

import { reuseMatchingPrimarySshBackend } from './connection-registry'
import {
  capturePrimarySshTeardown,
  createPrimaryRemoteRevalidator,
  ensureProbedPrimaryBackend
} from './primary-remote-revalidation'
import { RemoteLivenessTracker, RemoteRevalidationCoordinator } from './remote-liveness'
import { observePrimaryRevalidation, StageBuffer, withRegistryRevalidationStages } from './request-stages'

function setup() {
  const rows: any[] = []
  const output = new StageBuffer(async lines => {
    rows.push(
      ...lines
        .trim()
        .split('\n')
        .map(line => JSON.parse(line))
    )
  })

  const descriptor = {
    mode: 'remote',
    remoteKind: 'ssh',
    baseUrl: 'http://127.0.0.1:12345',
    ssh: { effectiveConfigFingerprint: 'fixture' }
  }

  const ensureBackend = vi.fn(async () => descriptor)
  const revalidatePrimaryRemoteBackend = vi.fn(async (_trigger?: string) => ({ ok: true, rebuilt: false }))

  const bindings = {
    readDesktopConnectionsRegistry: () => ({ primary: 'hub', connections: [{ id: 'hub', kind: 'ssh', label: 'Hub' }] }),
    registryDialConnectionId: (id: string) => id,
    registryReconnectService: { assertCanDispatch() {} },
    PROFILE_NAME_RE: /^[a-zA-Z0-9_-]+$/,
    remoteWarmSet: { noteDial() {} },
    managedSshConfig: () => ({}),
    effectiveSshConfigFingerprint: async () => 'fixture',
    reuseMatchingPrimarySshBackend,
    ensureProbedPrimaryBackend,
    ensureBackend,
    revalidatePrimaryRemoteBackend,
    observePrimaryRevalidation: (
      event: Parameters<typeof observePrimaryRevalidation>[0],
      fields: Parameters<typeof observePrimaryRevalidation>[1]
    ) => observePrimaryRevalidation(event, fields, output),
    withRegistryRevalidationStages: (
      opts: Parameters<typeof withRegistryRevalidationStages>[0],
      run: () => Promise<unknown>
    ) => withRegistryRevalidationStages(opts, run, output)
  }

  const source = ts.createSourceFile(
    'main.ts',
    fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8'),
    ts.ScriptTarget.Latest,
    true
  )
  const functions = source.statements.filter(
    n =>
      ts.isFunctionDeclaration(n) &&
      ['ensureRegistryBackend', 'ensureRegistryBackendUnobserved'].includes(n.name?.text || '')
  )

  const code = ts.transpileModule(
    functions.map(fn => fn.getText(source)).join('\n') + '\nreturn ensureRegistryBackend',
    {
      compilerOptions: { target: ts.ScriptTarget.ES2022 }
    }
  ).outputText

  const ensure = new Function(...Object.keys(bindings), code)(...Object.values(bindings))

  return { descriptor, ensure, ensureBackend, revalidatePrimaryRemoteBackend, rows, output, source }
}

test('registry forced probe reaches the matched shared primary instead of returning its unprobed descriptor', async () => {
  const f = setup()
  const result = await f.ensure('hub', 'default', '', { forceProbe: true })
  expect(f.revalidatePrimaryRemoteBackend).toHaveBeenCalledTimes(1)
  expect(f.revalidatePrimaryRemoteBackend).toHaveBeenCalledWith('registry')
  expect(result.baseUrl).toBe(f.descriptor.baseUrl)
})

test.each([{}, { passive: true, forceProbe: true }])(
  'ordinary/passive shared-primary ensure does not probe: %j',
  async opts => {
    const f = setup()
    expect((await f.ensure('hub', 'default', '', opts)).baseUrl).toBe(f.descriptor.baseUrl)
    expect(f.revalidatePrimaryRemoteBackend).not.toHaveBeenCalled()
    expect(f.ensureBackend).toHaveBeenCalledTimes(1)
  }
)

test('actual prewarm binding enters the observed registry path before any primary await', async () => {
  const f = setup()
  const declaration = f.source.statements.find(
    n =>
      ts.isVariableStatement(n) &&
      n.declarationList.declarations.some(d => d.name.getText(f.source) === 'remotePrewarmer')
  )!
  const code = ts.transpileModule(declaration.getText(f.source) + '\nreturn remotePrewarmer', {
    compilerOptions: { target: ts.ScriptTarget.ES2022 }
  }).outputText

  const warm = new Function('RemotePrewarmer', 'remoteWarmSet', 'ensureRegistryBackend', 'rememberLog', code)(
    class {
      constructor(readonly deps: any) {}
    },
    { listTargets() {} },
    f.ensure,
    () => {}
  )

  let release!: () => void
  const blocked = new Promise<void>(resolve => {
    release = resolve
  })
  f.ensureBackend.mockImplementation(async () => {
    await blocked

    return f.descriptor
  })
  const pending = warm.deps.ensure({ connectionId: 'hub', profile: 'default' }, { forceProbe: true })
  await f.output.flush()
  expect(f.rows).toHaveLength(1)
  expect(f.rows[0]).toMatchObject({ event: 'registry_start', trigger: 'prewarm', force_probe: true })
  release()
  await pending
  await f.output.flush()
  expect(f.revalidatePrimaryRemoteBackend).toHaveBeenCalledWith('prewarm')
  expect(f.rows.map(r => r.event)).toEqual(['registry_start', 'registry_match', 'matched_primary', 'registry_end'])
  expect(new Set(f.rows.map(r => r.invocation)).size).toBe(1)
})

test('actual main primary factory and reconnect IPC emit renderer round and probe verdict', async () => {
  const f = setup()
  const cp = Promise.resolve(f.descriptor)
  let handler!: () => Promise<unknown>
  const probe = vi.fn(async () => ({}))

  const bindings = {
    createPrimaryRemoteRevalidator,
    capturePrimarySshTeardown,
    observePrimaryRevalidation: (
      event: Parameters<typeof observePrimaryRevalidation>[0],
      fields: Parameters<typeof observePrimaryRevalidation>[1]
    ) => observePrimaryRevalidation(event, fields, f.output),
    backendConnectionState: { getPromise: () => cp },
    remoteRevalidation: new RemoteRevalidationCoordinator(),
    rememberLog() {},
    fetchJsonForBackend: probe,
    resetHermesConnectionState() {},
    remoteLiveness: new RemoteLivenessTracker(),
    sshConnections: new Map(),
    sshBootstrapCoordinator: { cancelAndWait: async (_scope: string, run: () => Promise<void>) => run() },
    teardownSshConnection: async () => {},
    backendPool: {},
    revalidatePool: async () => {},
    ipcMain: {
      handle: (_channel: string, callback: typeof handler) => {
        handler = callback
      }
    }
  }

  const nodes = f.source.statements.filter(
    n =>
      (ts.isVariableStatement(n) &&
        n.declarationList.declarations.some(d => d.name.getText(f.source) === 'revalidatePrimaryRemoteBackend')) ||
      (ts.isExpressionStatement(n) && n.getText(f.source).startsWith("ipcMain.handle('hermes:connection:revalidate'"))
  )

  const code = ts.transpileModule(nodes.map(n => n.getText(f.source)).join('\n'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022 }
  }).outputText
  new Function(...Object.keys(bindings), code)(...Object.values(bindings))
  await handler()
  await f.output.flush()
  expect(probe).toHaveBeenCalledWith(f.descriptor, '/api/status', { timeoutMs: 10_000, pool: 'probe' })
  expect(f.rows.find(r => r.event === 'invocation')).toMatchObject({ trigger: 'renderer', joined: false })
  expect(f.rows.some(r => r.event === 'probe_success')).toBe(true)
  expect(f.rows.some(r => r.event === 'ssh_capture' && r.matched === false)).toBe(true)
})
