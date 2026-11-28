import type { IpcMain } from 'electron'

import type { ManagedConnectionUpdateGate } from './managed-ssh-update'
import { createRegistryReconnectService } from './registry-reconnect-service'
import { proveRegistryOwnerStopped } from './registry-stopped-owner'
import { classifySshReuseProof, terminateOwnedDashboardForUpdate } from './remote-lifecycle'

interface RegistrySshState extends Omit<Parameters<typeof proveRegistryOwnerStopped>[1], 'profile'> {
  registryConnectionId: string
  primaryRegistryScope?: boolean
  remotePlatform: string
  remoteProfile?: string
  ssh: Parameters<typeof proveRegistryOwnerStopped>[0] & { close: () => Promise<void> }
}

interface WiringDeps<D, E extends { connectionPromise: Promise<D> }> {
  ipcMain: Pick<IpcMain, 'handle'>
  pool: Map<string, E>
  states: Map<string, RegistrySshState>
  gate: ManagedConnectionUpdateGate
  key: (id: string, profile: string) => string
  readRegistry: () => { connections: { id: string; kind: string }[] }
  dialClaims: { inFlight: (key: string) => unknown }
  poolStopper: { inFlight: (key: string) => unknown }
  bootstrap: { pending: { has: (key: string) => boolean } }
  request: Parameters<typeof createRegistryReconnectService<D, E, RegistrySshState>>[0]['request']
  keepalives: { stop: (key: string) => void }
  disposeTerminals: (key: string) => void
  log: (message: string) => void
  reopen: (id: string, profile: string, correlation: string) => Promise<unknown>
}

/** Main-process adapters; ownership and generation checks remain in the service. */
export function wireRegistryReconnect<D, E extends { connectionPromise: Promise<D> }>(deps: WiringDeps<D, E>) {
  const service = createRegistryReconnectService({
    pool: deps.pool,
    states: deps.states,
    gate: deps.gate,
    key: deps.key,
    isSsh: id => deps.readRegistry().connections.some(source => source.id === id && source.kind === 'ssh'),
    inFlight: key =>
      Boolean(deps.dialClaims.inFlight(key) || deps.poolStopper.inFlight(key) || deps.bootstrap.pending.has(key)),
    request: deps.request,
    proveStopped: state => proveRegistryOwnerStopped(state.ssh, { ...state, profile: state.remoteProfile || '' }),
    terminate: async state => {
      const expected = { ...state, profile: state.remoteProfile || '' }

      if (state.remotePlatform !== 'Linux') {
        throw new Error('Unsupported registry SSH platform')
      }
      await terminateOwnedDashboardForUpdate(state.ssh, expected)
    },
    close: async (key, state) => {
      deps.keepalives.stop(key)
      deps.disposeTerminals(key)
      // The owned serve is already confirmed stopped. Closing its transport
      // must never fall back to generic remote disconnect/kill.
      await state.ssh.close().catch(error => deps.log(`[registry-reconnect] transport close: ${String(error)}`))
    },
    onCleanupError: error => deps.log(`[registry-reconnect] cleanup: ${String(error)}`),
    reopen: deps.reopen
  })

  deps.ipcMain.handle('hermes:connections:reconnect-target', (_event, id, profile) => service.target(id, profile))
  deps.ipcMain.handle('hermes:connections:reconnect', (_event, target) => service.run(target))

  return {
    ...service,
    assertCanDial: (kind: string, id: string, correlation: string) => {
      if (kind === 'ssh') {
        deps.gate.assertCanDial(id, correlation)
      }
    }
  }
}

/** Authenticated stale owners are replaceable; transport uncertainty is not. */
export function createSshReuseProbe(fetchJson: (url: string, token: string) => Promise<unknown>) {
  return async (baseUrl: string, token: string, spawnNonce: string) => {
    try {
      const proof = await fetchJson(`${baseUrl}/api/ssh/ownership`, token)

      return classifySshReuseProof(proof, spawnNonce)
    } catch (error) {
      if (/^(401|403|404):/.test(String((error as Error)?.message || ''))) {
        return 'authenticated-stale'
      }

      throw error
    }
  }
}
