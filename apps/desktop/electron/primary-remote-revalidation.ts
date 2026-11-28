import {
  type RemoteLivenessTracker,
  type RemoteRevalidationCoordinator,
  revalidateRemoteConnection
} from './remote-liveness'

type RevalidationOptions = Parameters<typeof revalidateRemoteConnection>[0]

// Pin the transport identity, not the currently selected profile. Registry/global
// bootstraps use the empty SSH scope even when the active profile is named.
export function capturePrimarySshTeardown<T extends { localPort?: number }>(
  connection: { baseUrl?: string | null },
  deps: {
    states: ReadonlyMap<string, T>
    cancelAndWait: (scope: string, teardown: () => Promise<void>) => Promise<void>
    teardown: (scope: string) => Promise<void>
  }
) {
  const port = Number(new URL(connection.baseUrl!).port)
  const entry = [...deps.states].find(([, state]) => state.localPort === port)

  return async () => {
    if (!entry || deps.states.get(entry[0]) !== entry[1]) {return}
    await deps.cancelAndWait(entry[0], async () => {
      if (deps.states.get(entry[0]) === entry[1]) {await deps.teardown(entry[0])}
    })
  }
}

/** One primary policy for explicit warm checks and renderer reconnects. */
export function createPrimaryRemoteRevalidator(deps: {
  currentConnectionPromise: () => Promise<
    Awaited<RevalidationOptions['connectionPromise']> & { remoteKind?: string }
  > | null
  coordinator: RemoteRevalidationCoordinator
  probe: RevalidationOptions['probe']
  resetConnection: RevalidationOptions['resetConnection']
  tracker: RemoteLivenessTracker
  log: (line: string) => void
  captureSshTeardown: (
    connection: NonNullable<Awaited<RevalidationOptions['connectionPromise']>>
  ) => () => Promise<void>
}) {
  return async () => {
    const connectionPromise = deps.currentConnectionPromise()

    if (!connectionPromise) {
      return { ok: true, rebuilt: false }
    }

    // One failure observation per round, even with several tabs/prewarm aliases.
    return deps.coordinator.run(connectionPromise, async () => {
      const connection = await connectionPromise.catch(() => null)
      const teardown = connection?.remoteKind === 'ssh' ? deps.captureSshTeardown(connection) : null
      let retirement: Promise<void> | undefined

      const result = await revalidateRemoteConnection({
        ...deps,
        connectionPromise,
        resetConnection: () => {
          // Publish the SSH coordinator's drain synchronously, before clearing
          // the cached promise lets an ordinary dispatch start a replacement.
          retirement = teardown?.()
          deps.resetConnection()
        }
      })

      if (result.rebuilt) {
        // Await the captured old tunnel/master teardown before exposing a fresh boot.
        await retirement
      }

      return result
    })
  }
}

export async function ensureProbedPrimaryBackend<T>(
  cached: T,
  opts: { forceProbe?: boolean; passive?: boolean },
  deps: {
    revalidate: () => Promise<unknown>
    ensure: () => Promise<T>
  }
): Promise<T> {
  if (!opts.forceProbe || opts.passive) {
    return cached
  }

  await deps.revalidate()

  return deps.ensure()
}
