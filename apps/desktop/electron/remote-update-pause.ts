// While a remote host runs its own `hermes update`, the SSH preflight refuses to start a backend there
// (remote-lifecycle `assertRemoteInstallUpdateClear` throws kind 'update-in-progress'). Without memory of that
// refusal every renderer request and prewarm tick dialed a fresh SSH control master, ran the preflight, got the
// same refusal and closed it again: ~60 dials in 2 minutes against the hub on 2026-10-27. This gate remembers the
// refusal per remote install: callers fail fast from the cached error (no dial) until the backoff window opens,
// then exactly one caller probes; a successful dial clears the pause and the connection comes back once.
//
// Electron-free: main.ts wraps bootstrapSshConnection() (the one chokepoint every SSH dial goes through) in run().

export const UPDATE_PAUSE_BACKOFF_MS = [2_000, 5_000, 10_000, 20_000, 30_000]

interface PauseState {
  error: any
  failures: number
  pausedAt: number
  probing: boolean
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
          `[remote-update-pause] ${key} remote update finished; reconnected after ${state.failures} refused probe(s), paused ${this.#now() - state.pausedAt} ms`
        )
      }

      return result
    } catch (error: any) {
      if (error?.kind === 'update-in-progress' && !isRemoteUpdatePausedError(error)) {
        const existing = this.#states.get(key)

        // A dial that started before another caller recorded the pause (a parallel first dial for another profile
        // of the same host) confirms it; only the first refusal and each probe advance the backoff.
        if (!state && existing) {
          existing.error = error

          throw error
        }

        const current = existing ?? {
          error,
          failures: 0,
          pausedAt: this.#now(),
          probing: false,
          retryAt: 0
        }

        const delay = UPDATE_PAUSE_BACKOFF_MS[Math.min(current.failures, UPDATE_PAUSE_BACKOFF_MS.length - 1)]
        current.error = error
        current.failures += 1
        current.probing = false
        current.retryAt = this.#now() + delay
        this.#states.set(key, current)
        this.#log(
          `[remote-update-pause] ${key} ${current.failures === 1 ? 'paused' : 'still paused'} (${error?.message || error}); requests fail fast, next probe in ${delay} ms`
        )
      } else if (state && this.#states.get(key) === state) {
        // The probe failed some other way: the update gate no longer explains the failure, so stop answering for it.
        this.#states.delete(key)
      }

      throw error
    }
  }

  #pausedError(state: PauseState): Error {
    const error: any = new Error(state.error?.message || 'Remote Hermes update in progress; SSH startup is paused.')
    error.kind = 'update-in-progress'
    error.code = 'remote-update-paused'
    error.retryAt = state.retryAt
    error.cause = state.error

    return error
  }
}
