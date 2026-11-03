/**
 * Which published SSH-isolated scopes Desktop main keeps a keep-alive socket on.
 *
 * The keep-alive (ssh-isolated-keepalive.ts, #106935) stops a backend that Desktop
 * still owns from idle-exiting. Holding it for EVERY published scope turned any
 * one-off dial (a sidebar/roster sweep over all of a host's profiles) into a
 * backend that lived for the whole Desktop session: nine remote backends on one
 * host, 4 of them outside the bounded warm set remote-prewarm.ts promises
 * ("default + the profiles the user actually uses", cap per connection).
 *
 * A scope stays held while it is the primary, a bare/local/non-SSH scope, the
 * `default` profile, a prewarm target, or was dialled for use in the last
 * ACTIVE_USE_HOLD_MS. Everything else only loses Desktop main's keep-alive
 * socket: the tunnel and pool entry stay, a renderer socket or running turn
 * still keeps the backend up, and a later dispatch probe redials a backend that
 * idle-exited. Nothing here kills a process or tears down a connection.
 *
 * Electron-free: main.ts injects the registry, targets and clock.
 */
import { parseBackendScopeKey } from './connection-registry'

export const ACTIVE_USE_HOLD_MS = 30 * 60_000
export const HOLD_RECONCILE_MS = 15_000

type KeepaliveRegistry = {
  isHeld: (scope: string) => boolean
  scopes: () => string[]
  setHeld: (scope: string, held: boolean) => boolean
}

export type SshKeepaliveHoldDeps = {
  keepalives: KeepaliveRegistry
  /** `${connectionId}::${profile}` of every current prewarm target (remote-prewarm.ts prewarmTargetKey). */
  prewarmKeys: () => Iterable<string>
  isPrimaryScope: (scope: string) => boolean
  isSshConnection: (connectionId: string) => boolean
  log?: (line: string) => void
  now?: () => number
  setTimer?: (fn: () => void, ms: number) => unknown
  clearTimer?: (timer: unknown) => void
}

export class SshKeepaliveHolds {
  readonly #deps: SshKeepaliveHoldDeps
  readonly #lastUse = new Map<string, number>()
  #timer: unknown = null

  constructor(deps: SshKeepaliveHoldDeps) {
    this.#deps = deps
  }

  #now() {
    return (this.#deps.now ?? Date.now)()
  }

  /** A non-passive dial for real use (same signal remote-prewarm's recent set records). */
  noteUse(scope: string) {
    this.#lastUse.set(scope, this.#now())
    // Re-hold now, not at the next reconcile: a released backend near the end of its idle grace must not exit
    // between this dial and the next tick.
    this.#deps.keepalives.setHeld(scope, true)
  }

  /** Post-resume rebuild of a retired pool scope: a dead scope outside the warm/in-use set is only retired, since
   * rebuilding it would re-pin it. */
  async rebuildIfHeld(scope: string, redial: (scope: string) => Promise<unknown>): Promise<boolean> {
    if (this.shouldHold(scope)) {
      await redial(scope)

      return true
    }

    this.#deps.log?.(`[ssh] resume: retired ${scope} without rebuild (outside warm/in-use set)`)

    return false
  }

  shouldHold(scope: string, prewarm: ReadonlySet<string> = new Set(this.#deps.prewarmKeys())): boolean {
    const { connectionId, profile } = parseBackendScopeKey(scope)

    if (!connectionId || !this.#deps.isSshConnection(connectionId) || profile === 'default') {
      return true
    }

    if (this.#deps.isPrimaryScope(scope) || prewarm.has(`${connectionId}::${profile}`)) {
      return true
    }

    const usedAt = this.#lastUse.get(scope)

    return usedAt != null && this.#now() - usedAt < ACTIVE_USE_HOLD_MS
  }

  reconcile() {
    const prewarm = new Set(this.#deps.prewarmKeys())
    const live = new Set(this.#deps.keepalives.scopes())

    for (const scope of this.#lastUse.keys()) {
      if (!live.has(scope) && !this.shouldHold(scope, prewarm)) {
        this.#lastUse.delete(scope)
      }
    }

    for (const scope of live) {
      const held = this.shouldHold(scope, prewarm)

      if (this.#deps.keepalives.setHeld(scope, held)) {
        this.#deps.log?.(
          held
            ? `[ssh] keep-alive held again for ${scope} (in use / warm set)`
            : `[ssh] keep-alive released for ${scope}: not primary, default, prewarm or used in ${ACTIVE_USE_HOLD_MS / 60_000} min; the backend idle-exits on its own once no client or turn holds it`
        )
      }
    }
  }

  start() {
    if (this.#timer != null) {
      return
    }

    const setTimer = this.#deps.setTimer ?? ((fn: () => void, ms: number) => setInterval(fn, ms))

    const timer = setTimer(() => this.reconcile(), HOLD_RECONCILE_MS)

    ;(timer as { unref?: () => void } | undefined)?.unref?.()
    this.#timer = timer
  }

  stop() {
    if (this.#timer == null) {
      return
    }

    ;(this.#deps.clearTimer ?? (t => clearInterval(t as ReturnType<typeof setInterval>)))(this.#timer)
    this.#timer = null
  }
}
