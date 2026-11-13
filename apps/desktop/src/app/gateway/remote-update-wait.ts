// [daveey] A remote host running its own `hermes update` refuses SSH startup until the update process exits
// (electron/remote-lifecycle.ts assertRemoteInstallUpdateClear, windows-remote-lifecycle.ts, and main's
// remote-update-pause gate, which answers from the cached refusal without dialing until its backoff opens).
// That refusal crosses IPC as plain text, e.g.
//   "Error invoking remote method 'hermes:connection:for': Error: Remote Hermes update process 1779396 is still
//    running; SSH startup is paused."
// so the renderer recognises it by the phrase every producer shares (electron/remote-update-refusal.ts).
//
// A remote update routinely outlasts the bounded boot retry (5 attempts, about a minute), and the
// hermes:connection:for path does not mark main's boot snapshot retryable at all, so boot used to land on the
// "Hermes couldn't start" overlay and stay there after the update finished until the user pressed Retry. The boot
// loop instead keeps polling on this schedule, outside the bounded budget, until the update ends or the wait cap.

import { REMOTE_UPDATE_PAUSED_PHRASE } from '../../../electron/remote-update-refusal'

// Poll interval. Main's gate fails fast (no SSH dial) until its own backoff window opens (2 s → 30 s), so a short
// renderer poll costs one IPC round-trip, and recovery lands within one gate step of the update exiting.
export const REMOTE_UPDATE_POLL_MS = 5_000

// An update that pauses SSH startup longer than this is stuck, not finishing: surface the recovery overlay.
export const REMOTE_UPDATE_WAIT_MAX_MS = 30 * 60_000

export function isRemoteUpdatePausedBootError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error ?? '')

  return message.includes(REMOTE_UPDATE_PAUSED_PHRASE)
}

// Tracks one continuous remote-update wait for the boot loop. `next()` returns the delay before the next poll, or
// null once the wait has run past the cap (the caller then fails boot as usual). The cap is hard: the wait stays
// spent until `reset()`, which the caller makes only on a successful boot or a failure that is not this refusal,
// so a bounded retry after the cap that meets the same refusal never starts a fresh 30 min wait.
export class RemoteUpdateWait {
  #startedAt: null | number = null
  readonly #now: () => number

  constructor(now: () => number = Date.now) {
    this.#now = now
  }

  next(): null | number {
    const now = this.#now()

    if (this.#startedAt === null) {
      this.#startedAt = now
    }

    return now - this.#startedAt >= REMOTE_UPDATE_WAIT_MAX_MS ? null : REMOTE_UPDATE_POLL_MS
  }

  reset(): void {
    this.#startedAt = null
  }
}
