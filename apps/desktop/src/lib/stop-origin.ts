/**
 * [daveey] The gesture that sent a Stop. Rides `session.interrupt` as `origin`; the gateway logs it on the
 * `session.interrupt:` line and keeps it in the turn's `turn_stopped` notice metadata, so "why did this
 * stop?" has an answer in agent.log. Without it every Stop door sent an identical RPC.
 *
 * - `stop_button`: the composer's Stop button
 * - `esc`: Esc in the chat (composer Esc-to-cancel)
 * - `tile`: a split tile's Stop (button or Esc; `tile:esc` when the tile composer's Esc sent it)
 * - `slash`: the `/stop` command
 * - `delete`: deleting a chat whose turn is running
 * - `send_now`: send-now on a queued prompt while busy (interrupts to reach the queue)
 * - `transcript`: the streaming message's own stop (assistant runtime cancel)
 * - `rewind`: edit / reload / restore interrupting the live turn first
 * - `plugin:<id>`: a desktop plugin
 *
 * The gateway accepts any value (unknown ones are kept, made log-safe), so adding one needs no backend change.
 */
export type StopOrigin =
  | 'delete'
  | 'esc'
  | 'rewind'
  | 'send_now'
  | 'slash'
  | 'stop_button'
  | 'tile'
  | 'tile:esc'
  | 'transcript'
  | `plugin:${string}`

/** `session.interrupt` params carrying the gesture. `origin` is typed `unknown` because cancel handlers are
 *  also wired straight to `onClick`, which passes a MouseEvent: anything but a non-empty string is dropped. */
export function interruptParams(sessionId: string, origin?: unknown): { origin?: string; session_id: string } {
  return typeof origin === 'string' && origin.trim() ? { origin, session_id: sessionId } : { session_id: sessionId }
}

export function asStopOrigin(origin: unknown): StopOrigin | undefined {
  return typeof origin === 'string' && origin.trim() ? (origin as StopOrigin) : undefined
}
