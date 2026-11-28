import { type PrimaryRevalidationObserver, type PrimaryRevalidationTrigger, revalidationIdentity, revalidationPort, revalidationScopeIdentity, withRevalidationRound } from './primary-revalidation-observation'
import {
  type RemoteLivenessTracker,
  type RemoteRevalidationCoordinator,
  type RemoteRevalidationResult,
  revalidateRemoteConnection
} from './remote-liveness'

type RevalidationOptions = Parameters<typeof revalidateRemoteConnection>[0]

// Pin the transport identity, not the currently selected profile. Registry/global
// bootstraps use the empty SSH scope even when the active profile is named.
export function capturePrimarySshTeardown<T extends { localPort?: number }>(
  connection: { baseUrl?: string | null },
  deps: {
    states: ReadonlyMap<string, T>
    log?: (line: string) => void
    observe?: PrimaryRevalidationObserver
    cancelAndWait: (scope: string, teardown: () => Promise<void>) => Promise<void>
    teardown: (scope: string) => Promise<void>
  }
) {
  const port = Number(new URL(connection.baseUrl!).port)
  const entry = [...deps.states].find(([, state]) => state.localPort === port)
  const fields = { port, state: entry ? revalidationIdentity(entry[1]) : 0, scope: entry ? revalidationScopeIdentity(entry[0]) : '' }
  deps.observe?.('ssh_capture', { ...fields, matched: Boolean(entry) })

  return async () => {
    if (!entry) {
      deps.observe?.('missing_scope', fields)
      deps.log?.(`[remote] primary SSH retirement skipped: no tracked tunnel for local port ${port}`)

      return
    }

    if (deps.states.get(entry[0]) !== entry[1]) {
      deps.observe?.('stale_scope', fields)

      return
    }

    deps.observe?.('drain_start', fields)
    await deps.cancelAndWait(entry[0], async () => {
      if (deps.states.get(entry[0]) === entry[1]) {
        deps.observe?.('teardown_start', fields)

        try {
          await deps.teardown(entry[0])
          deps.observe?.('teardown_end', fields)
        } catch (error) {
          deps.observe?.('teardown_error', fields)
          throw error
        }
      } else {deps.observe?.('stale_scope_after_drain', fields)}
    })
    deps.observe?.('drain_end', fields)
  }
}

/** One primary policy for explicit warm checks and renderer reconnects. */
export function createPrimaryRemoteRevalidator(deps: {
  observe?: PrimaryRevalidationObserver
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
  const seen = new WeakSet<object>()

  return async (trigger: PrimaryRevalidationTrigger = 'direct') => {
    const connectionPromise = deps.currentConnectionPromise()

    if (!connectionPromise) {
      deps.observe?.('no_connection', { trigger })

      return { ok: true, rebuilt: false }
    }

    // One failure observation per round, even with several tabs/prewarm aliases.
    const generation = revalidationIdentity(connectionPromise)

    const pending: Promise<RemoteRevalidationResult> = deps.coordinator.run(connectionPromise, async () => withRevalidationRound({ round: revalidationIdentity(pending), generation }, async () => {
      deps.observe?.('round_start', { trigger })

      try {
        const connection = await connectionPromise.catch(() => null)
        const teardown = connection?.remoteKind === 'ssh' ? deps.captureSshTeardown(connection) : null
        let retirement: Promise<void> | undefined

        const result = await revalidateRemoteConnection({
          ...deps,
          connectionPromise,
          resetConnection: () => {
            // Arm the drain before clearing the promise permits a replacement.
            deps.observe?.('retirement_requested')
            retirement = teardown?.()
            deps.resetConnection()
            deps.observe?.('reset')
          }
        })

        if (result.rebuilt) { await retirement }
        deps.observe?.('round_end', { rebuilt: result.rebuilt, port: revalidationPort(connection?.baseUrl) })

        return result
      } catch (error) {
        deps.observe?.('round_error')
        throw error
      }
    }))

    deps.observe?.('invocation', { trigger, generation, round: revalidationIdentity(pending), joined: seen.has(pending) })
    seen.add(pending)

    return pending
  }
}

export async function ensureProbedPrimaryBackend<T>(
  cached: T,
  opts: { forceProbe?: boolean; passive?: boolean },
  deps: {
    revalidate: () => Promise<unknown>
    ensure: () => Promise<T>
    observe?: PrimaryRevalidationObserver
  }
): Promise<T> {
  deps.observe?.('matched_primary', { matched: true, force_probe: Boolean(opts.forceProbe), passive: Boolean(opts.passive), skipped: !opts.forceProbe || Boolean(opts.passive) })

  if (!opts.forceProbe || opts.passive) {
    return cached
  }

  await deps.revalidate()

  return deps.ensure()
}
