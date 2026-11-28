import { randomUUID } from 'node:crypto'

import type { ManagedConnectionUpdateGate } from './managed-ssh-update'
import { createRegistryReconnector, type RegistryReconnectTarget } from './registry-reconnect'
import { createRegistryRetirementClient } from './registry-retirement-http'

interface PoolEntry<D> {
  connectionPromise: Promise<D>
}
interface OwnedState {
  registryConnectionId: string
  primaryRegistryScope?: boolean
  spawnNonce: string
  remotePlatform: string
}
interface ServiceDeps<D, E extends PoolEntry<D>, S extends OwnedState> {
  pool: Map<string, E>
  states: Map<string, S>
  gate: ManagedConnectionUpdateGate
  key: (connectionId: string, profile: string) => string
  isSsh: (connectionId: string) => boolean
  inFlight: (key: string) => boolean
  request: Parameters<typeof createRegistryRetirementClient<D>>[0]
  proveStopped: (state: S) => Promise<boolean>
  terminate: (state: S) => Promise<void>
  close: (key: string, state: S) => Promise<void>
  onCleanupError: (error: unknown) => void
  reopen: (connectionId: string, profile: string, correlation: string) => Promise<unknown>
}

/** The connection gate fences registry edits and all bootstrap publications;
 * the coordinator fences only this resident's teardown and uncertain retries.
 */
export function createRegistryReconnectService<D, E extends PoolEntry<D>, S extends OwnedState>(
  deps: ServiceDeps<D, E, S>
) {
  interface Resident {
    entry: E
    state: S
    descriptor: Promise<D>
    generation: string
    key: string
  }
  const residents = new WeakMap<E, Resident>()
  const correlations = new Map<string, string>()
  const claimedProfiles = new Map<string, string>()
  const client = createRegistryRetirementClient(deps.request)

  function current(connectionId: string, profile: string): Resident | undefined {
    const key = deps.key(connectionId, profile)
    const entry = deps.pool.get(key)
    const state = deps.states.get(key)

    // Only Linux has runtime-independent, identity-bound termination today.
    // Windows uses the possibly deleted managed Python; Darwin lacks pidfd.
    // Refuse before committing an irreversible backend admission fence.
    if (!state || state.remotePlatform !== 'Linux') {
      return undefined
    }

    if (
      !deps.isSsh(connectionId) ||
      !entry ||
      !state ||
      state.primaryRegistryScope ||
      state.registryConnectionId !== connectionId ||
      !state.spawnNonce ||
      deps.inFlight(key)
    ) {
      return undefined
    }

    let resident = residents.get(entry)

    if (!resident || resident.state !== state || resident.descriptor !== entry.connectionPromise) {
      resident = { entry, state, descriptor: entry.connectionPromise, key, generation: randomUUID() }
      residents.set(entry, resident)
    }

    return resident
  }

  async function cleanup(resident: Resident) {
    if (
      deps.pool.get(resident.key) !== resident.entry ||
      deps.states.get(resident.key) !== resident.state ||
      resident.entry.connectionPromise !== resident.descriptor
    ) {
      throw new Error('Registry backend changed during termination')
    }

    deps.pool.delete(resident.key)
    deps.states.delete(resident.key)

    try {
      await deps.close(resident.key, resident.state)
    } catch (error) {
      deps.onCleanupError(error)
    }
  }

  const coordinator = createRegistryReconnector<Resident>({
    current,
    generation: resident => resident.generation,
    proveOwned: async resident => client.proveOwned(await resident.descriptor, resident.state.spawnNonce),
    prepare: async resident => client.prepare(await resident.descriptor),
    commit: async (resident, token) => client.commit(await resident.descriptor, token),
    cancel: async (resident, token) => client.cancel(await resident.descriptor, token),
    recoverStopped: async resident => {
      if (!(await deps.proveStopped(resident.state))) {
        return false
      }
      await cleanup(resident)

      return true
    },
    terminate: async resident => {
      await deps.terminate(resident.state)
      await cleanup(resident)
    },
    reopen: async (connectionId, profile) => {
      await deps.reopen(connectionId, profile, correlations.get(connectionId)!)
      const replacement = current(connectionId, profile)

      if (!replacement) {
        throw new Error('Registry backend reconnect did not publish an owned resident')
      }

      return replacement.generation
    }
  })

  return {
    target: (connectionId: string, profile: string): RegistryReconnectTarget | null => {
      const resident = current(connectionId, profile)

      return resident ? { connectionId, profile, generation: resident.generation } : null
    },
    assertCanDispatch: (connectionId: string, profile: string, correlation = '') => {
      // Our connection-wide claim protects configuration and new publication,
      // not dispatch to already resident, unrelated profiles (including primary).
      const held = correlations.get(connectionId)

      if (held && deps.gate.owner(connectionId) === held && claimedProfiles.get(connectionId) !== profile) {
        return
      }
      deps.gate.assertCanDial(connectionId, correlation)
    },
    assertCanStop: (key: string) => {
      for (const [connectionId, profile] of claimedProfiles) {
        if (deps.key(connectionId, profile) === key) {
          throw new Error('Registry backend reconnect in progress')
        }
      }
    },
    run: async (target: RegistryReconnectTarget) => {
      if (
        !target ||
        typeof target.connectionId !== 'string' ||
        typeof target.profile !== 'string' ||
        typeof target.generation !== 'string'
      ) {
        return { ok: false }
      }

      const resident = current(target.connectionId, target.profile)

      if (!resident || resident.generation !== target.generation) {
        return { ok: false }
      }
      const held = correlations.get(target.connectionId)

      if (held && claimedProfiles.get(target.connectionId) !== target.profile) {
        return { ok: false }
      }
      const correlation: string = held || randomUUID()

      if (!held) {
        if (!deps.gate.claim(target.connectionId, correlation)) {
          return { ok: false }
        }
        correlations.set(target.connectionId, correlation)
        claimedProfiles.set(target.connectionId, target.profile)
      }

      try {
        return await coordinator.run(target)
      } finally {
        // A failed/unknown commit must not reopen normal admission. Only the
        // exact-generation retry can reconcile it; never cancel on ambiguity.
        let clear = false

        try {
          coordinator.assertCanDial(target.connectionId, target.profile)
          clear = true
        } catch {
          /* fenced */
        }

        if (clear) {
          correlations.delete(target.connectionId)
          claimedProfiles.delete(target.connectionId)
          deps.gate.release(target.connectionId, correlation)
        }
      }
    }
  }
}
