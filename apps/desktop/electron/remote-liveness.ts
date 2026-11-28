import { isMissingHealthEndpointError } from './backend-health'
import type { PrimaryRevalidationObserver } from './primary-revalidation-observation'

export const REMOTE_LIVENESS_TIMEOUT_MS = 10_000
// Dispatch is synchronous user intent: a cached descriptor must prove its
// forwarded endpoint is alive before it can be returned. Probe cheap
// /api/health — not /api/status, whose cold payload (gateway probe, topology,
// state.db session count) on a fresh SSH forward routinely runs seconds — and
// reuse the background liveness budget so a quiet-box cold-start (~6-8s) can
// finish instead of failing the probe and kicking off a reconnect storm.
export const POOLED_REMOTE_DISPATCH_PROBE_TIMEOUT_MS = REMOTE_LIVENESS_TIMEOUT_MS
export const REMOTE_LIVENESS_FAILURE_LIMIT = 3
// Even at the capped retry path, consecutive liveness observations are at most
// about 48s apart (ticket mint + socket open + backoff + the next status probe).
// One minute keeps a continuous outage together without carrying old failures.
export const REMOTE_LIVENESS_FAILURE_WINDOW_MS = 60_000
// Pooled remote backends are probed from the renderer's reconnect IPC, which
// fires on a minutes-long cadence once the primary connection is healthy — far
// beyond REMOTE_LIVENESS_FAILURE_WINDOW_MS. With the primary's 60s window a
// dead pooled descriptor's failure streak resets on every tick, the drop path
// is never reached, and the pool serves the dead descriptor until app restart
// (every call fails with ECONNRESET). A successful probe still clears the
// streak via recordSuccess(), so a wide window cannot produce false drops.
export const REMOTE_POOLED_LIVENESS_FAILURE_WINDOW_MS = 15 * 60_000

// A dispatch skips its /api/health probe when this backend answered a request within this window: the probe
// would only re-prove what the last answer already proved, and it costs a tunnel round trip on every send.
export const DISPATCH_PROBE_SKIP_WINDOW_MS = 30_000

/** When each remote backend (by its forwarded baseUrl) last answered a request successfully. */
export class RemoteAnswerLedger {
  readonly #lastAnswerAt = new Map<string, number>()
  readonly #now: () => number

  constructor(now: () => number = Date.now) {
    this.#now = now
  }

  recordAnswer(baseUrl: null | string | undefined): void {
    if (baseUrl) {
      this.#lastAnswerAt.set(baseUrl, this.#now())
    }
  }

  forget(baseUrl: null | string | undefined): void {
    if (baseUrl) {
      this.#lastAnswerAt.delete(baseUrl)
    }
  }

  answeredWithin(baseUrl: null | string | undefined, windowMs: number): boolean {
    const at = baseUrl ? this.#lastAnswerAt.get(baseUrl) : undefined

    return at !== undefined && windowMs > 0 && this.#now() - at < windowMs
  }
}

export const remoteAnswerLedger = new RemoteAnswerLedger()

export interface RemoteLivenessFailure {
  failures: number
  shouldReset: boolean
}

interface RemoteConnectionDescriptor {
  baseUrl?: null | string
  mode?: null | string
}

export interface RevalidateRemoteConnectionOptions<TConnection extends RemoteConnectionDescriptor> {
  observe?: PrimaryRevalidationObserver
  connectionPromise: Promise<TConnection>
  currentConnectionPromise: () => null | Promise<TConnection>
  log: (message: string) => void
  probe: (connection: TConnection, path: string, options: { timeoutMs: number }) => Promise<unknown>
  resetConnection: () => void
  tracker: RemoteLivenessTracker
}

export interface RemoteRevalidationResult {
  ok: true
  rebuilt: boolean
}

/**
 * Coalesces revalidation work for one cached connection promise.
 *
 * Every Desktop BrowserWindow owns a renderer gateway loop. When several
 * windows observe the same disconnect they can all ask the Electron main
 * process to revalidate the shared primary connection at once. Those calls
 * must count as one probe, not several consecutive failures.
 */
export class RemoteRevalidationCoordinator {
  readonly #inflightByConnection = new WeakMap<object, Promise<unknown>>()

  run<T>(connection: object, task: () => Promise<T>): Promise<T> {
    const existing = this.#inflightByConnection.get(connection) as Promise<T> | undefined

    if (existing) {
      return existing
    }

    const pending = Promise.resolve().then(task)

    const clear = () => {
      if (this.#inflightByConnection.get(connection) === pending) {
        this.#inflightByConnection.delete(connection)
      }
    }

    this.#inflightByConnection.set(connection, pending)
    // Clean up on both outcomes without creating an unhandled rejected branch.
    void pending.then(clear, clear)

    return pending
  }
}

interface EnsureHealthyPooledRemoteBackendForDispatchOptions<TConnection extends RemoteConnectionDescriptor> {
  connectionPromise: Promise<TConnection>
  currentConnectionPromise: () => null | Promise<TConnection>
  // The probe asks for pool 'probe': its own small socket pool, so a JSON pool saturated by a slow link can't fail
  // the health verdict and tear down a tunnel that answers.
  probe: (connection: TConnection, path: string, options: { timeoutMs: number; pool?: 'probe' }) => Promise<unknown>
  reconnect: () => Promise<TConnection>
  retire: (error: unknown) => Promise<void> | void
  // Skip the probe when the backend answered within skipWindowMs (0 forces it, e.g. the prewarm keepalive).
  answers?: RemoteAnswerLedger
  skipWindowMs?: number
  onProbe?: (outcome: 'ran' | 'skipped' | 'slow', elapsedMs: number) => void
  // [daveey] Slow-link tolerance (slow-link.ts). A probe that TIMES OUT while the transport is still alive means
  // the link is slow, not that the backend is gone: retiring it only makes the reconnect refetch everything over
  // the same slow link. Such a descriptor is kept until noteTimeout reports giveUp.
  slowLink?: {
    isTimeout: (error: unknown) => boolean
    transportAlive: () => Promise<boolean>
    noteTimeout: (connection: TConnection) => { giveUp: boolean }
  }
}

/**
 * Gate dispatch through a cheap health probe of the exact cached descriptor.
 *
 * A failed descriptor is retired before reconnecting, while identity checks
 * prevent a late probe from tearing down a replacement installed by another
 * caller. The caller should single-flight this function per cached promise so
 * concurrent dispatches share one retire/reconnect sequence.
 */
export async function ensureHealthyPooledRemoteBackendForDispatch<TConnection extends RemoteConnectionDescriptor>({
  connectionPromise,
  currentConnectionPromise,
  probe,
  reconnect,
  retire,
  answers,
  skipWindowMs = DISPATCH_PROBE_SKIP_WINDOW_MS,
  onProbe,
  slowLink
}: EnsureHealthyPooledRemoteBackendForDispatchOptions<TConnection>): Promise<TConnection> {
  let connection: TConnection | undefined
  const startedAt = Date.now()

  try {
    connection = await connectionPromise

    if (currentConnectionPromise() !== connectionPromise) {
      return reconnect()
    }

    if (answers?.answeredWithin(connection.baseUrl, skipWindowMs)) {
      onProbe?.('skipped', Date.now() - startedAt)

      return connection
    }

    try {
      await probe(connection, '/api/health', {
        timeoutMs: POOLED_REMOTE_DISPATCH_PROBE_TIMEOUT_MS,
        pool: 'probe'
      })
    } catch (healthError) {
      // A remote that predates /api/health would otherwise 404 every dispatch,
      // retire the tunnel and reconnect forever; the boot probe falls back the
      // same way (backend-health.ts).
      if (!isMissingHealthEndpointError(healthError)) {
        throw healthError
      }

      await probe(connection, '/api/status', {
        timeoutMs: POOLED_REMOTE_DISPATCH_PROBE_TIMEOUT_MS,
        pool: 'probe'
      })
    }
  } catch (error) {
    if (
      slowLink &&
      connection &&
      currentConnectionPromise() === connectionPromise &&
      slowLink.isTimeout(error) &&
      (await slowLink.transportAlive().catch(() => false)) &&
      currentConnectionPromise() === connectionPromise &&
      !slowLink.noteTimeout(connection).giveUp
    ) {
      // Slow, not down: keep the descriptor and let the request go out with its stretched timeout. The answer
      // mark is set so the dispatches queued behind this one do not each pay another probe timeout; a request
      // that times out forgets it again (remote-down-gate.ts), and the next dispatch probes and counts a strike.
      answers?.recordAnswer(connection.baseUrl)
      onProbe?.('slow', Date.now() - startedAt)

      return connection
    }

    answers?.forget(connection?.baseUrl)

    if (currentConnectionPromise() === connectionPromise) {
      await retire(error)
    }

    return reconnect()
  }

  answers?.recordAnswer(connection.baseUrl)
  onProbe?.('ran', Date.now() - startedAt)

  if (currentConnectionPromise() !== connectionPromise) {
    return reconnect()
  }

  return connection
}

/**
 * Tracks consecutive remote liveness failures independently per gateway.
 * A successful probe clears the streak, and reaching the limit consumes it so
 * a rebuilt connection starts from a clean state.
 */
export class RemoteLivenessTracker {
  readonly #failureLimit: number
  readonly #failureWindowMs: number
  readonly #failuresByBaseUrl = new Map<string, { failures: number; lastFailureAt: number }>()
  readonly #now: () => number

  constructor(
    failureLimit = REMOTE_LIVENESS_FAILURE_LIMIT,
    failureWindowMs = REMOTE_LIVENESS_FAILURE_WINDOW_MS,
    now: () => number = Date.now
  ) {
    if (!Number.isInteger(failureLimit) || failureLimit < 1) {
      throw new Error('Remote liveness failure limit must be a positive integer.')
    }

    if (!Number.isFinite(failureWindowMs) || failureWindowMs < 1) {
      throw new Error('Remote liveness failure window must be positive.')
    }

    this.#failureLimit = failureLimit
    this.#failureWindowMs = failureWindowMs
    this.#now = now
  }

  recordSuccess(baseUrl: string): void {
    this.#failuresByBaseUrl.delete(baseUrl)
  }

  recordFailure(baseUrl: string): RemoteLivenessFailure {
    const now = this.#now()
    const previous = this.#failuresByBaseUrl.get(baseUrl)
    const withinFailureWindow = previous && now - previous.lastFailureAt <= this.#failureWindowMs
    const failures = (withinFailureWindow ? previous.failures : 0) + 1
    const shouldReset = failures >= this.#failureLimit

    if (shouldReset) {
      this.#failuresByBaseUrl.delete(baseUrl)
    } else {
      this.#failuresByBaseUrl.set(baseUrl, { failures, lastFailureAt: now })
    }

    return { failures, shouldReset }
  }

  clear(): void {
    this.#failuresByBaseUrl.clear()
  }
}

export interface PooledRemoteEntry<TConnection extends RemoteConnectionDescriptor = RemoteConnectionDescriptor> {
  connectionPromise?: null | Promise<TConnection>
  process?: unknown
  remoteBaseUrl?: null | string
}

export interface RevalidatePooledRemoteBackendsOptions<TConnection extends RemoteConnectionDescriptor> {
  entries: Iterable<[string, PooledRemoteEntry<TConnection>]>
  log: (message: string) => void
  probe: (connection: TConnection, path: string, options: { timeoutMs: number }) => Promise<unknown>
  stopBackend: (profile: string) => void
  tracker: RemoteLivenessTracker
}

/**
 * Probe pooled REMOTE descriptors and drop the dead ones.
 *
 * A pooled entry is a cached descriptor with no child process, so nothing
 * signals its host's death. Without this the pool serves a descriptor for an
 * unreachable host indefinitely.
 *
 * Entries share the primary's failure policy, keyed per base URL, so a profile
 * pointing at the same host as another does not burn the streak twice as fast.
 */
export async function revalidatePooledRemoteBackends<TConnection extends RemoteConnectionDescriptor>({
  entries,
  log,
  probe,
  stopBackend,
  tracker
}: RevalidatePooledRemoteBackendsOptions<TConnection>): Promise<{ dropped: string[] }> {
  const remotes = [...entries].filter(([, entry]) => !entry.process && entry.remoteBaseUrl)
  const dropped: string[] = []

  await Promise.all(
    remotes.map(async ([profile, entry]) => {
      const baseUrl = String(entry.remoteBaseUrl).replace(/\/+$/, '')

      try {
        if (!entry.connectionPromise) {
          throw new Error('Remote backend descriptor is unavailable.')
        }

        const connection = await entry.connectionPromise
        await probe(connection, '/api/status', { timeoutMs: REMOTE_LIVENESS_TIMEOUT_MS })
        tracker.recordSuccess(baseUrl)
      } catch {
        const failure = tracker.recordFailure(baseUrl)

        if (!failure.shouldReset) {
          log(
            `Pooled remote backend for profile "${profile}" failed liveness probe (${failure.failures}/${REMOTE_LIVENESS_FAILURE_LIMIT}); keeping descriptor for retry.`
          )

          return
        }

        log(`Pooled remote backend for profile "${profile}" failed liveness probe; dropping stale descriptor.`)
        stopBackend(profile)
        dropped.push(profile)
      }
    })
  )

  return { dropped }
}

export interface RevalidateSuspectPooledRemoteBackendsOptions<TConnection extends RemoteConnectionDescriptor> {
  entries: Iterable<[string, PooledRemoteEntry<TConnection>]>
  log: (message: string) => void
  probe: (connection: TConnection, path: string, options: { timeoutMs: number }) => Promise<unknown>
  /** Re-dial a retired pool key so the tunnel is rebuilt eagerly, not on the next click. */
  rebuild: (poolKey: string) => Promise<unknown>
  /** Tear down the dead descriptor (pool entry + SSH tunnel/master) for this key. */
  retire: (poolKey: string) => Promise<void> | void
  tracker: RemoteLivenessTracker
}

/**
 * Post-resume sweep of pooled REMOTE descriptors (#93910).
 *
 * After a sleep/wake or network restore every pooled SSH tunnel is suspect:
 * the SSH master died with the network, but the local forward's descriptor is
 * still cached. Unlike
 * the background policy in revalidatePooledRemoteBackends — which tolerates a
 * failure streak because transient blips are common in steady state — a
 * suspect descriptor that fails ONE bounded probe after resume is dead: retire
 * it immediately and rebuild, instead of serving "Gateway offline" through two
 * more failure rounds.
 *
 * Bounded by construction: one probe per remote entry per invocation, retire
 * and rebuild each awaited once; the caller coalesces invocations and applies
 * the resume holdoff, so there is no polling loop here. A failed retire skips
 * the rebuild (never dial on top of a descriptor that is still installed) and
 * a failed rebuild is logged and left for the renderer's normal reconnect
 * path — fail closed, never throw out of the sweep.
 */
export async function revalidateSuspectPooledRemoteBackends<TConnection extends RemoteConnectionDescriptor>({
  entries,
  log,
  probe,
  rebuild,
  retire,
  tracker
}: RevalidateSuspectPooledRemoteBackendsOptions<TConnection>): Promise<{ rebuilt: string[]; retired: string[] }> {
  const remotes = [...entries].filter(([, entry]) => !entry.process && entry.remoteBaseUrl)
  const rebuilt: string[] = []
  const retired: string[] = []

  await Promise.all(
    remotes.map(async ([poolKey, entry]) => {
      const baseUrl = String(entry.remoteBaseUrl).replace(/\/+$/, '')

      try {
        if (!entry.connectionPromise) {
          throw new Error('Remote backend descriptor is unavailable.')
        }

        const connection = await entry.connectionPromise
        await probe(connection, '/api/status', { timeoutMs: REMOTE_LIVENESS_TIMEOUT_MS })
        tracker.recordSuccess(baseUrl)

        return
      } catch (probeError) {
        log(
          `Pooled remote backend "${poolKey}" failed its post-resume probe (${probeError instanceof Error ? probeError.message : String(probeError)}); rebuilding tunnel.`
        )
      }

      try {
        await retire(poolKey)
      } catch (retireError) {
        // The dead entry may still be installed; rebuilding on top of it could
        // double-dial one scope. Leave it — the dispatch-time probe retires it
        // on the next use.
        log(
          `Pooled remote backend "${poolKey}" could not be retired after resume (${retireError instanceof Error ? retireError.message : String(retireError)}); leaving descriptor for dispatch-time recovery.`
        )

        return
      }

      retired.push(poolKey)
      // The rebuilt tunnel must start from a clean failure state; stale
      // pre-sleep failures should not count against the fresh descriptor.
      tracker.recordSuccess(baseUrl)

      try {
        await rebuild(poolKey)
        rebuilt.push(poolKey)
      } catch (rebuildError) {
        log(
          `Pooled remote backend "${poolKey}" could not be rebuilt after resume (${rebuildError instanceof Error ? rebuildError.message : String(rebuildError)}); renderer reconnect will retry.`
        )
      }
    })
  )

  return { rebuilt, retired }
}

// macOS fires 'resume' and 'unlock-screen' near-simultaneously on wake, and a
// flapping Wi-Fi association can restore the network several times in a few
// seconds. One sweep per window is enough: the sweep itself probes every
// remote entry, and the renderer's revalidate IPC covers anything that dies
// later. Keep this comfortably above the dispatch probe timeout so overlapping
// signals can never queue back-to-back sweeps into a hot loop.
export const POWER_RESUME_REVALIDATION_HOLDOFF_MS = 15_000

export interface AttachPowerResumeRemoteRevalidationOptions {
  log: (message: string) => void
  now?: () => number
  // Method syntax (bivariant) so Electron's overloaded PowerMonitor.on
  // satisfies this structural seam while tests can pass a tiny fake.
  powerMonitor: { on(event: 'resume' | 'unlock-screen', listener: () => void): unknown }
  revalidate: () => Promise<unknown>
}

/**
 * Wire the suspect-pool sweep to the Electron powerMonitor seam (#93910).
 *
 * Returns the trigger so tests (and the network-restore nudge, if main ever
 * wants one) can drive the exact code path the events run. The trigger is a
 * plain function: holdoff first (one sweep per wake window, never a hot
 * loop), then a fire-and-forget revalidation whose rejection is logged and
 * swallowed — a broken sweep must never take down the resume handler or wedge
 * future wakes.
 */
export function attachPowerResumeRemoteRevalidation({
  log,
  now = Date.now,
  powerMonitor,
  revalidate
}: AttachPowerResumeRemoteRevalidationOptions): () => Promise<void> {
  let lastKickAt: null | number = null

  const trigger = async (): Promise<void> => {
    const at = now()

    if (lastKickAt !== null && at - lastKickAt < POWER_RESUME_REVALIDATION_HOLDOFF_MS) {
      return
    }

    lastKickAt = at

    try {
      await revalidate()
    } catch (error) {
      log(
        `Post-resume remote revalidation failed (${error instanceof Error ? error.message : String(error)}); will retry on the next wake or renderer reconnect.`
      )
    }
  }

  powerMonitor.on('resume', () => void trigger())
  powerMonitor.on('unlock-screen', () => void trigger())

  return trigger
}

/**
 * Probe the cached primary remote connection and apply the failure policy.
 * The caller owns single-flight coordination; identity checks here ensure an
 * old async result cannot mutate or reset a replacement connection.
 */
export async function revalidateRemoteConnection<TConnection extends RemoteConnectionDescriptor>({
  connectionPromise,
  currentConnectionPromise,
  log,
  probe,
  resetConnection,
  tracker,
  observe
}: RevalidateRemoteConnectionOptions<TConnection>): Promise<RemoteRevalidationResult> {
  let connection: TConnection

  try {
    connection = await connectionPromise
  } catch {
    // The cached boot already rejected; its own recovery path will clear it.
    observe?.('connection_rejected')

    return { ok: true, rebuilt: false }
  }

  if (currentConnectionPromise() !== connectionPromise) {
    observe?.('stale_connection')

    return { ok: true, rebuilt: false }
  }

  if (connection.mode !== 'remote' || !connection.baseUrl) {
    observe?.('not_remote')

    return { ok: true, rebuilt: false }
  }

  const baseUrl = connection.baseUrl.replace(/\/+$/, '')
  const started = performance.now()

  try {
    observe?.('probe_start')
    await probe(connection, '/api/status', { timeoutMs: REMOTE_LIVENESS_TIMEOUT_MS })

    if (currentConnectionPromise() !== connectionPromise) {
      observe?.('stale_connection', { duration_ms: performance.now() - started })

      return { ok: true, rebuilt: false }
    }

    tracker.recordSuccess(baseUrl)
    observe?.('probe_success', { failures: 0, duration_ms: performance.now() - started })

    return { ok: true, rebuilt: false }
  } catch {
    if (currentConnectionPromise() !== connectionPromise) {
      observe?.('stale_connection', { duration_ms: performance.now() - started })

      return { ok: true, rebuilt: false }
    }

    const failure = tracker.recordFailure(baseUrl)
    observe?.('probe_failure', {
      failures: failure.failures,
      should_reset: failure.shouldReset,
      duration_ms: performance.now() - started
    })

    if (!failure.shouldReset) {
      log(
        `Cached remote Hermes backend failed liveness probe (${failure.failures}/${REMOTE_LIVENESS_FAILURE_LIMIT}); keeping connection for retry.`
      )

      return { ok: true, rebuilt: false }
    }

    log('Cached remote Hermes backend failed liveness probe; dropping stale connection.')
    resetConnection()

    return { ok: true, rebuilt: true }
  }
}
