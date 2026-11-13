import type { ClientSessionState } from '@/app/types'

export type LiveTurnVerdict = 'ended' | 'running' | 'unknown'

export interface LiveTurnStatusResponse {
  sessions?: { id?: string; last_active?: number | null; status?: string }[]
}

/** What one `session.active_list` snapshot says about `runtimeId`'s turn. A
 *  runtime missing from a well-formed list has been reaped: its turn is over.
 *  `starting` is an agent build for a turn the backend accepted. */
export function liveTurnVerdict(
  response: LiveTurnStatusResponse | null | undefined,
  runtimeId: string
): LiveTurnVerdict {
  if (!Array.isArray(response?.sessions)) {
    return 'unknown'
  }

  const status = response.sessions.find(session => session.id?.trim() === runtimeId)?.status

  if (status === undefined || status === 'idle') {
    return 'ended'
  }

  return status === 'starting' || status === 'waiting' || status === 'working' ? 'running' : 'unknown'
}

/** [daveey] When `runtimeId`'s turn last did anything, per the backend (ms), or
 *  null when the snapshot doesn't say (older backend, runtime absent). */
export function liveTurnLastActiveMs(
  response: LiveTurnStatusResponse | null | undefined,
  runtimeId: string
): null | number {
  const lastActive = response?.sessions?.find(session => session.id?.trim() === runtimeId)?.last_active
  const ms = Number(lastActive) * 1000

  return lastActive != null && Number.isFinite(ms) && ms > 0 ? ms : null
}

/** [daveey] The fields a turn claim is read from. */
export type TurnClaimFields = Pick<ClientSessionState, 'awaitingResponse' | 'busy' | 'turnLive'>

/** [daveey] Whether `state` holds a turn claim: busy, awaiting a reply, or a
 *  live turn. The single copy of this predicate — reconnect reconcile, the
 *  tile delegate's retireBusyClaim and the update-held idle check
 *  (`holdsUpdateHeldPromptIdle`) must agree on it (PR #1913). A full
 *  `ClientSessionState` (or undefined) gets a type guard; a bare field pick
 *  gets a plain boolean, since its negation must not narrow it to `never`. */
/* eslint-disable no-redeclare -- overload signatures; the rule predates TS */
export function holdsTurnClaim(state: ClientSessionState | undefined): state is ClientSessionState
export function holdsTurnClaim(state: TurnClaimFields | undefined): boolean

export function holdsTurnClaim(state: TurnClaimFields | undefined): boolean {
  return Boolean(state && (state.busy || state.awaitingResponse || state.turnLive))
}
/* eslint-enable no-redeclare */

export function isLiveTurnAwaitingEvents(state: ClientSessionState | undefined): boolean {
  return holdsTurnClaim(state) && !state.needsInput
}
