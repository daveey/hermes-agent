// Hold every remote gateway warm: at startup, dial each SSH connection in connections.json (SSH ControlMaster +
// tunnel + backend reuse), then keep it up with a forced health probe every tickMs and a backoff reconnect when it
// drops. A dispatch then finds a pooled, recently-answered descriptor and never waits on an SSH dial or a spawn.
//
// Electron-free: main.ts injects the registry read, the ensure call and the clock.

export const PREWARM_TICK_MS = 15_000
export const PREWARM_BACKOFF_MS = [2_000, 5_000, 15_000, 30_000, 60_000]

export interface RemotePrewarmDeps {
  // SSH connection ids from connections.json, re-read every tick so added/removed connections converge.
  listConnectionIds: () => string[]
  // Dial or revalidate one connection; `forceProbe` bypasses the dispatch probe skip so a dropped tunnel is noticed.
  ensure: (connectionId: string, opts: { forceProbe: boolean }) => Promise<unknown>
  log: (line: string) => void
  now?: () => number
  setTimer?: (fn: () => void, ms: number) => { unref?: () => void }
  clearTimer?: (timer: unknown) => void
  tickMs?: number
}

interface ConnectionState {
  failures: number
  nextAttemptAt: number
  warm: boolean
}

export class RemotePrewarmer {
  readonly #deps: RemotePrewarmDeps
  readonly #states = new Map<string, ConnectionState>()
  readonly #inflight = new Set<string>()
  #timer: unknown = null
  #stopped = true

  constructor(deps: RemotePrewarmDeps) {
    this.#deps = deps
  }

  get running(): boolean {
    return !this.#stopped
  }

  isWarm(connectionId: string): boolean {
    return this.#states.get(connectionId)?.warm === true
  }

  // Dial every connection now (in parallel) and start the keepalive loop. Resolves when the first round settles.
  async start(): Promise<void> {
    if (!this.#stopped) {
      return
    }

    this.#stopped = false
    await this.tick(true)
    this.#schedule()
  }

  stop(): void {
    this.#stopped = true

    if (this.#timer) {
      ;(this.#deps.clearTimer ?? clearTimeout)(this.#timer as any)
      this.#timer = null
    }
  }

  // One round. The first round dials (no probe to force); later rounds force the probe on warm connections.
  async tick(initial = false): Promise<void> {
    const now = (this.#deps.now ?? Date.now)()
    const ids = this.#deps.listConnectionIds()

    for (const known of [...this.#states.keys()]) {
      if (!ids.includes(known)) {
        this.#states.delete(known)
      }
    }

    await Promise.all(
      ids.map(async id => {
        const state = this.#states.get(id) ?? { failures: 0, nextAttemptAt: 0, warm: false }
        this.#states.set(id, state)

        if (this.#inflight.has(id) || now < state.nextAttemptAt || this.#stopped) {
          return
        }

        this.#inflight.add(id)
        const startedAt = (this.#deps.now ?? Date.now)()

        try {
          await this.#deps.ensure(id, { forceProbe: !initial && state.warm })

          if (!state.warm || state.failures) {
            this.#deps.log(
              `[remote-prewarm] ${id} ${state.failures ? 'reconnected' : 'warm'} in ${(this.#deps.now ?? Date.now)() - startedAt} ms`
            )
          }

          state.warm = true
          state.failures = 0
          state.nextAttemptAt = 0
        } catch (error: any) {
          const delay = PREWARM_BACKOFF_MS[Math.min(state.failures, PREWARM_BACKOFF_MS.length - 1)]
          state.warm = false
          state.failures += 1
          state.nextAttemptAt = (this.#deps.now ?? Date.now)() + delay
          this.#deps.log(
            `[remote-prewarm] ${id} unavailable (${error?.message || error}); retry ${state.failures} in ${delay} ms`
          )
        } finally {
          this.#inflight.delete(id)
        }
      })
    )
  }

  #schedule(): void {
    if (this.#stopped) {
      return
    }

    const setTimer = this.#deps.setTimer ?? ((fn: () => void, ms: number) => setTimeout(fn, ms))

    const timer = setTimer(() => {
      void this.tick().finally(() => this.#schedule())
    }, this.#deps.tickMs ?? PREWARM_TICK_MS)

    ;(timer as { unref?: () => void } | undefined)?.unref?.()
    this.#timer = timer
  }
}
