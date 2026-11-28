export interface RegistryReconnectTarget {
  connectionId: string
  profile: string
  generation: string
}

export interface RegistryReconnectDeps<E> {
  current: (connectionId: string, profile: string) => E | undefined
  generation: (entry: E) => string
  proveOwned: (entry: E) => Promise<boolean>
  prepare: (entry: E) => Promise<string | null>
  commit: (entry: E, token: string) => Promise<boolean>
  cancel: (entry: E, token: string) => Promise<void>
  recoverStopped: (entry: E) => Promise<boolean>
  terminate: (entry: E) => Promise<void>
  reopen: (connectionId: string, profile: string, entry: E) => Promise<string>
}

export function createRegistryReconnector<E>(deps: RegistryReconnectDeps<E>) {
  const active = new Set<string>()
  const uncertain = new Map<string, E>()
  const committed = new Map<string, E>()
  const keyFor = (connectionId: string, profile: string) => JSON.stringify([connectionId, profile])

  async function finish(target: RegistryReconnectTarget, key: string, entry: E, stopped = false) {
    if (!stopped) {
      await deps.terminate(entry)
    }
    uncertain.delete(key)
    committed.delete(key)

    // Remote termination awaits I/O; never reopen over a newer resident.
    const current = deps.current(target.connectionId, target.profile)

    if (current && (current !== entry || deps.generation(current) !== target.generation)) {
      return { ok: false }
    }

    return { ok: true, generation: await deps.reopen(target.connectionId, target.profile, entry) }
  }

  async function recycle(target: RegistryReconnectTarget) {
    const key = keyFor(target.connectionId, target.profile)
    const entry = deps.current(target.connectionId, target.profile)

    if (uncertain.has(key) && uncertain.get(key) !== entry) {
      return { ok: false }
    }

    if (!entry || deps.generation(entry) !== target.generation) {
      return { ok: false }
    }

    // A confirmed commit survives serve exit. The terminator re-proves the
    // exact SSH process identity or confirms that captured owner has exited.
    if (committed.get(key) === entry) {
      return finish(target, key, entry)
    }

    // Ambiguity never grants live termination authority. This path only
    // removes the captured resident after read-only proof it has stopped.
    if (uncertain.get(key) === entry && (await deps.recoverStopped(entry))) {
      return finish(target, key, entry, true)
    }

    if (!(await deps.proveOwned(entry))) {
      return { ok: false }
    }

    const token = await deps.prepare(entry)

    if (!token) {
      return { ok: false }
    }

    let mayBeCommitted = false

    try {
      if (deps.current(target.connectionId, target.profile) !== entry || deps.generation(entry) !== target.generation) {
        return { ok: false }
      }

      mayBeCommitted = true
      uncertain.set(key, entry)
      const didCommit = await deps.commit(entry, token)

      if (!didCommit) {
        mayBeCommitted = false
        uncertain.delete(key)

        return { ok: false }
      }

      committed.set(key, entry)

      if (deps.current(target.connectionId, target.profile) !== entry || deps.generation(entry) !== target.generation) {
        return { ok: false }
      }

      return await finish(target, key, entry)
    } finally {
      if (!mayBeCommitted) {
        await deps.cancel(entry, token)
      }
    }
  }

  return {
    assertCanDial: (connectionId: string, profile: string) => {
      const key = keyFor(connectionId, profile)

      if (active.has(key) || uncertain.has(key)) {
        throw new Error('Registry backend reconnect in progress')
      }
    },
    run: async (target: RegistryReconnectTarget) => {
      if (
        ![target?.connectionId, target?.profile, target?.generation].every(
          value => typeof value === 'string' && value.length > 0 && value.trim() === value
        )
      ) {
        return { ok: false }
      }

      const key = keyFor(target.connectionId, target.profile)

      if (active.has(key)) {
        return { ok: false }
      }

      active.add(key)

      try {
        return await recycle(target)
      } finally {
        active.delete(key)
      }
    }
  }
}
