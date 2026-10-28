// While a remote host runs its own `hermes update`, the SSH preflight refuses to start a backend there
// (remote-lifecycle `assertRemoteInstallUpdateClear` throws kind 'update-in-progress'). Without memory of that
// refusal every renderer request and prewarm tick dialed a fresh SSH control master, ran the preflight, got the
// same refusal and closed it again: ~60 dials in 2 minutes against the hub on 2026-10-27. This gate remembers the
// refusal per remote install: callers fail fast from the cached error (no dial) until the backoff window opens,
// then exactly one caller probes; a successful dial clears the pause and the connection comes back once.
//
// The same gate covers a remote that cannot be dialed at all (host down, connection refused, an SSH timeout,
// rejected credentials): each renderer request used to open its own control master and wait out the 15 s SSH
// timeout (11 dials and 36 stalled requests to the mini in 2 min on 2026-10-27). See classifyRemoteDialFailure for
// which failures are remembered and why.
//
// Electron-free: main.ts wraps bootstrapSshConnection() (the one chokepoint every SSH dial goes through) in run().

export const UPDATE_PAUSE_BACKOFF_MS = [2_000, 5_000, 10_000, 20_000, 30_000]
// Same steps as remote-prewarm's own backoff for a down host, so prewarm's schedule is unchanged by the gate.
export const UNREACHABLE_BACKOFF_MS = [2_000, 5_000, 15_000, 30_000, 60_000]

export type RemoteDialFailureClass = 'unreachable' | 'update'

// SSH dial failures the gate remembers as 'unreachable'. Each is decided outside this process and a redial seconds
// later gets the same answer, but the remote can fix itself (or the user fixes it outside Desktop: ssh-add,
// known_hosts, a Tailscale check), so a bounded probe keeps looking:
// - network: 'unreachable' (resolve/refused/no route), 'timeout' (dial or exec hit the SSH timeout),
//   'transient-transport-error' (the transport dropped mid-preflight);
// - SSH refused us: 'auth-failed', 'interactive-auth', 'host-key-changed'. Boot latches these as terminal on the
//   primary path; the gate only keeps pool and prewarm dials from repeating them per request.
// Everything else passes through uncached: 'ssh-client-failed' (#103288, raised before the gate and latched by
// boot), 'superseded' (a caller cancelled), and failures after a successful dial that the next attempt is meant to
// act on (ownership, lockfile skew, spawn, ready timeout, missing profile or hermes). Untagged and 'unknown'
// errors pass too: an unrecognised failure must never be masked by a cached one.
const UNREACHABLE_KINDS = new Set([
  'unreachable',
  'timeout',
  'transient-transport-error',
  'auth-failed',
  'interactive-auth',
  'host-key-changed'
])

export function classifyRemoteDialFailure(error: any): RemoteDialFailureClass | null {
  if (isRemoteUpdatePausedError(error) || isRemoteUnreachableBackoffError(error)) {
    return null
  }

  if (error?.kind === 'update-in-progress') {
    return 'update'
  }

  return UNREACHABLE_KINDS.has(error?.kind) ? 'unreachable' : null
}

interface PauseState {
  error: any
  failures: number
  pausedAt: number
  probing: boolean
  reason: RemoteDialFailureClass
  retryAt: number
}

// One remote install: the update marker lives under the remote hermes home, shared by every profile on the host.
export function remoteUpdatePauseKey(config: {
  host?: string
  port?: number | string
  remoteHermesPath?: string
  user?: string
}): string {
  return `${config.user || ''}@${config.host || ''}:${config.port || 22}|${config.remoteHermesPath || ''}`
}

export function isRemoteUpdatePausedError(error: any): boolean {
  return error?.code === 'remote-update-paused'
}

export function isRemoteUnreachableBackoffError(error: any): boolean {
  return error?.code === 'remote-unreachable-backoff'
}

export class RemoteUpdatePause {
  readonly #states = new Map<string, PauseState>()
  readonly #now: () => number
  readonly #log: (line: string) => void

  constructor(opts: { log: (line: string) => void; now?: () => number }) {
    this.#log = opts.log
    this.#now = opts.now ?? Date.now
  }

  paused(key: string): boolean {
    return this.#states.has(key)
  }

  reason(key: string): RemoteDialFailureClass | null {
    return this.#states.get(key)?.reason ?? null
  }

  // An explicit user action (Reload and retry, apply or save a connection, a reachable Test) dials right away.
  clear(why: string): void {
    if (this.#states.size === 0) {
      return
    }

    const keys = [...this.#states.keys()]
    this.#states.clear()
    this.#log(`[remote-dial-backoff] cleared ${keys.join(', ')} for ${why}; the next request dials now`)
  }

  async run<T>(key: string, dial: () => Promise<T>): Promise<T> {
    const state = this.#states.get(key)

    if (state) {
      if (state.probing || this.#now() < state.retryAt) {
        throw this.#pausedError(state)
      }

      state.probing = true
    }

    try {
      const result = await dial()

      if (state && this.#states.get(key) === state) {
        this.#states.delete(key)
        this.#log(
          state.reason === 'update'
            ? `[remote-update-pause] ${key} remote update finished; reconnected after ${state.failures} refused probe(s), paused ${this.#now() - state.pausedAt} ms`
            : `[remote-dial-backoff] ${key} reachable again; reconnected after ${state.failures} failed dial(s), down ${this.#now() - state.pausedAt} ms`
        )
      }

      return result
    } catch (error: any) {
      const reason = classifyRemoteDialFailure(error)

      if (reason) {
        const existing = this.#states.get(key)

        // A dial that started before another caller recorded the pause (a parallel first dial for another profile
        // of the same host) confirms it; only the first failure and each probe advance the backoff.
        if (!state && existing) {
          if (existing.reason === reason) {
            existing.error = error
          }

          error.retryAt = existing.retryAt

          throw error
        }

        const current =
          existing && existing.reason === reason
            ? existing
            : { error, failures: 0, pausedAt: this.#now(), probing: false, reason, retryAt: 0 }

        const schedule = reason === 'update' ? UPDATE_PAUSE_BACKOFF_MS : UNREACHABLE_BACKOFF_MS
        const delay = schedule[Math.min(current.failures, schedule.length - 1)]
        current.error = error
        current.failures += 1
        current.probing = false
        current.retryAt = this.#now() + delay
        // Callers that pace themselves (remote-prewarm) wait for the same window the gate opens.
        error.retryAt = current.retryAt
        this.#states.set(key, current)
        this.#log(
          reason === 'update'
            ? `[remote-update-pause] ${key} ${current.failures === 1 ? 'paused' : 'still paused'} (${error?.message || error}); requests fail fast, next probe in ${delay} ms`
            : `[remote-dial-backoff] ${key} ${current.failures === 1 ? 'unreachable' : 'still unreachable'} (${error?.kind}: ${error?.message || error}); requests fail fast, next probe in ${delay} ms`
        )
      } else if (state && this.#states.get(key) === state) {
        // The probe failed some other way: the cached failure no longer explains it, so stop answering for it.
        this.#states.delete(key)
      }

      throw error
    }
  }

  #pausedError(state: PauseState): Error {
    if (state.reason === 'unreachable') {
      // Same message and kind as the dial failure, so boot's classifiers (auth, host key, retryable) read it alike.
      const error: any = new Error(state.error?.message || 'Remote host is unreachable over SSH.')
      error.kind = state.error?.kind
      error.sshError = state.error?.sshError
      error.code = 'remote-unreachable-backoff'
      error.retryAt = state.retryAt
      error.cause = state.error

      return error
    }

    const error: any = new Error(state.error?.message || 'Remote Hermes update in progress; SSH startup is paused.')
    error.kind = 'update-in-progress'
    error.code = 'remote-update-paused'
    error.retryAt = state.retryAt
    error.cause = state.error

    return error
  }
}
