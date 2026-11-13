import type { ClientSessionState } from '@/app/types'

/** [daveey] A turn running on runtime R for stored session S proves that every OTHER runtime of S holding a
 *  blocking prompt is dead: the backend runs one turn per stored session, and a respawned backend re-mints runtime
 *  ids (startup resume auto-continues the session on a fresh one). Reconnect leaves `needsInput` alone on purpose
 *  (on a same-backend blip the prompt is still live) and only the active gateway's active_list poll reaps vanished
 *  runtimes, so a clarify open when a REMOTE backend restarted kept its stored session in `$attentionSessionIds`
 *  forever: a `needs-input` dot and a red `stuck` stripe with no question anywhere (selfheal desktop-54ae373894).
 *  Only a sibling no longer running is retired (reconnect already downgraded its busy claim); a sibling still
 *  busy, turn-live or awaiting a response is left to its own terminal events. Returns the runtime ids whose
 *  prompt `publishSessionState` clears. */
export function supersededPromptRuntimes(
  states: Readonly<Record<string, ClientSessionState | undefined>>,
  runtimeId: string,
  state: ClientSessionState
): string[] {
  const storedId = state.storedSessionId

  if (!storedId || !(state.busy || state.turnLive)) {
    return []
  }

  return Object.entries(states)
    .filter(
      ([otherId, other]) =>
        otherId !== runtimeId &&
        other?.needsInput &&
        !other.busy &&
        !other.turnLive &&
        !other.awaitingResponse &&
        other.storedSessionId === storedId
    )
    .map(([otherId]) => otherId)
}
