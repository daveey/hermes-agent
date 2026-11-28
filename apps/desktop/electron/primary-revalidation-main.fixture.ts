// Execute the real main-process functions without booting Electron. Only the
// external dial/UI and fixture-owned server lifetime are adapters, not reset/close.
import fs from 'node:fs'

import ts from 'typescript'

import { createBackendConnectionState } from './backend-connection-state'
import { teardownSshState } from './connection-apply'
import { connectionScopeKey, resolveProfileBackendRoute } from './connection-config'
import { reuseMatchingPrimarySshBackend } from './connection-registry'
import { joinPrimaryBoot, primaryBootRequest } from './primary-boot-join'
import {
  capturePrimarySshTeardown,
  createPrimaryRemoteRevalidator,
  ensureProbedPrimaryBackend
} from './primary-remote-revalidation'
import { RemoteLivenessTracker, RemoteRevalidationCoordinator, revalidateRemoteConnection } from './remote-liveness'
import { RemotePrewarmer } from './remote-prewarm'
import { createRemoteWarmSet } from './remote-warm-set'
import { observePrimaryRevalidation, withRegistryRevalidationStages } from './request-stages'
import { createBootstrapCoordinator } from './ssh-bootstrap-coordinator'
import type { SshConnection } from './ssh-connection'
import { createSshTeardownTracker } from './ssh-teardown'
import { remoteCleanupFor, remoteTeardownAction } from './ssh-teardown-policy'

type FixtureDial = { ssh: SshConnection; port: number; remotePort: number }

export function primaryRevalidationMainFixture({
  dial,
  read,
  log,
  probeTimeoutMs = 150
}: {
  dial: (attempt: number) => Promise<FixtureDial>
  read: (url: string, options: any) => Promise<any>
  log: (message: string) => void
  probeTimeoutMs?: number
}) {
  const state = createBackendConnectionState<never, any>()
  const sshConnections = new Map()

  const source = ts.createSourceFile(
    'main.ts',
    fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8'),
    ts.ScriptTarget.Latest,
    true
  )

  const names = [
    'sshScopeKey',
    'ensureBackend',
    'freshGatewayWsUrl',
    'ensureRegistryBackend',
    'ensureRegistryBackendUnobserved',
    'revalidatePrimaryRemoteBackend',
    'resetHermesConnectionState',
    'invalidatePrimaryConnection',
    'teardownSshConnection'
  ]

  // Missing new declarations are omitted so source-only reversion reaches the
  // behavioral old-master-still-alive failure, not a fixture import/setup error.
  const declarations = names
    .map(
      name => source.statements.find(n => ts.isFunctionDeclaration(n) && n.name?.text === name)?.getText(source) || ''
    )
    .join('\n')

  const prewarmer = ['revalidatePrimaryRemoteBackend', 'remotePrewarmer']
    .map(
      name =>
        source.statements
          .find(
            n => ts.isVariableStatement(n) && n.declarationList.declarations.some(d => d.name.getText(source) === name)
          )
          ?.getText(source) || ''
    )
    .join('\n')

  let dials = 0
  let probes = 0
  const probePools: unknown[] = []
  const sshBootstrapCoordinator = createBootstrapCoordinator()

  const boot = () => {
    if (state.getPromise()) {
      return state.getPromise()!
    }

    const attempt = state.startAttempt()

    const pending = sshBootstrapCoordinator.start('', 'fixture', async () => {
      const c = await dial(dials++)
      // Registry/global SSH bootstrap is keyed by sshScopeKey(null), not the
      // active profile name. Preserve this real namespace boundary in the fixture.
      sshConnections.set('', {
        ssh: c.ssh,
        localPort: c.port,
        remotePort: c.remotePort,
        ownershipId: 'fixture',
        primaryRegistryScope: true
      })

      return {
        mode: 'remote',
        remoteKind: 'ssh',
        baseUrl: `http://127.0.0.1:${c.port}`,
        wsUrl: `ws://127.0.0.1:${c.port}/api/ws`,
        ssh: { effectiveConfigFingerprint: 'fixture' }
      }
    })

    state.setPromise(attempt, pending)

    return pending
  }

  const unexpectedPool = () => {
    throw Error('unexpected pooled spawn')
  }

  const registry = () => ({
    version: 2 as const,
    primary: 'hub',
    connections: [{ id: 'hub', kind: 'ssh' as const, label: 'Fixture', host: 'fixture.invalid' }]
  })

  const bindings = {
    observePrimaryRevalidation,
    withRegistryRevalidationStages,
    capturePrimarySshTeardown,
    createPrimaryRemoteRevalidator,
    ensureProbedPrimaryBackend,
    backendConnectionState: state,
    readDesktopConnectionsRegistry: registry,
    registryDialConnectionId: (id: string) => id,
    registryReconnectService: { assertCanDispatch() {}, assertCanStop() {} },
    PROFILE_NAME_RE: /^[a-zA-Z0-9_-]+$/,
    RemotePrewarmer,
    remoteWarmSet: createRemoteWarmSet({
      statePath: () => '',
      readRegistry: registry,
      keepalives: { sync() {} } as any,
      isPrimaryScope: () => true,
      log
    }),
    managedSshConfig: () => ({}),
    effectiveSshConfigFingerprint: async () => 'fixture',
    reuseMatchingPrimarySshBackend,
    primaryBootRequest,
    joinPrimaryBoot,
    resolveProfileBackendRoute,
    primaryProfileKey: () => 'default',
    profileDeletionGate: { assertCanStart() {} },
    profileRouteOptions: () => ({ primaryProfile: 'default', primaryRemoteActive: true }),
    startHermes: boot,
    setWslBridgeProfileState() {},
    backendPool: new Map(),
    assertNotPassiveSpawn: unexpectedPool,
    newPoolEntry: unexpectedPool,
    dialPoolBackend: unexpectedPool,
    logPoolSpawnFailure() {},
    forgetFailedPoolEntry() {},
    rememberRemoteWsHeaders() {},
    remoteRevalidation: new RemoteRevalidationCoordinator(),
    remoteLiveness: new RemoteLivenessTracker(),
    revalidateRemoteConnection,
    rememberLog: log,
    fetchJsonForBackend: (c: any, path: string, opts: any) => {
      probes++
      probePools.push(opts.pool)

      return read(c.baseUrl + path, { ...opts, timeoutMs: probeTimeoutMs })
    },
    primaryProfilePin: { clear() {} },
    resetBootProgressForReconnect() {
      throw Error('unexpected hard reset')
    },
    sshBootstrapCoordinator,
    connectionScopeKey,
    sshConnections,
    sshIsolatedKeepalives: { stop() {} },
    terminalIpc: { disposeTerminalSessionsForSshScope() {} },
    remoteTeardownAction,
    remoteCleanupFor,
    remoteLifecycle: {
      async release() {},
      async disconnect() {
        throw Error('unexpected remote terminate')
      }
    },
    sshRememberLog: log,
    sshTeardowns: createSshTeardownTracker(),
    teardownSshState,
    sshOwnershipKey: () => 'fixture'
  }

  const js = ts.transpileModule(
    'let backendStartFailure=null,remoteReauthFailure=null;\n' +
      declarations +
      '\n' +
      prewarmer +
      '\nreturn {ensure:ensureRegistryBackend,wsUrl:freshGatewayWsUrl,prewarmer:remotePrewarmer}',
    { compilerOptions: { target: ts.ScriptTarget.ES2022 } }
  ).outputText

  const api = new Function(...Object.keys(bindings), js)(...Object.values(bindings))

  return { ...api, stats: () => ({ dials, probes, probePools, masterStates: sshConnections.size }), state }
}
