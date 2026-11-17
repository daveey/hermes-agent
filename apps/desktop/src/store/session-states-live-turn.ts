import type { ClientSessionState } from '@/app/types'

export type LiveTurnVerdict = 'ended' | 'running' | 'unknown'

export interface LiveTurnStatusResponse {
  sessions?: { id?: string; last_active?: number | null; session_key?: string; status?: string }[]
}

type LiveTurnRow = NonNullable<LiveTurnStatusResponse['sessions']>[number]

/** The snapshot row for this chat's turn: the one carrying `runtimeId`, else
 *  the one whose `session_key` is the chat's stored id. A backend that keys
 *  its rows by stored id (the session authority) has no row under a runtime
 *  id, and reading that as "reaped" painted the no-reply card on a turn that
 *  was still running. */
function liveTurnRow(
  response: LiveTurnStatusResponse | null | undefined,
  runtimeId: string,
  storedSessionId?: null | string
): { by: 'id' | 'session_key'; row: LiveTurnRow } | null {
  const sessions = Array.isArray(response?.sessions) ? response.sessions : []
  const byId = sessions.find(session => session.id?.trim() === runtimeId)

  if (byId) {
    return { by: 'id', row: byId }
  }

  const stored = storedSessionId?.trim()
  const byKey = stored ? sessions.find(session => session.session_key?.trim() === stored) : undefined

  return byKey ? { by: 'session_key', row: byKey } : null
}

/** What one `session.active_list` snapshot says about `runtimeId`'s turn. A
 *  chat missing from a well-formed list has been reaped: its turn is over.
 *  `starting` is an agent build for a turn the backend accepted. */
export function liveTurnVerdict(
  response: LiveTurnStatusResponse | null | undefined,
  runtimeId: string,
  storedSessionId?: null | string
): LiveTurnVerdict {
  if (!Array.isArray(response?.sessions)) {
    return 'unknown'
  }

  const status = liveTurnRow(response, runtimeId, storedSessionId)?.row.status

  if (status === undefined || status === 'idle') {
    return 'ended'
  }

  return status === 'starting' || status === 'waiting' || status === 'working' ? 'running' : 'unknown'
}

/** [daveey] When `runtimeId`'s turn last did anything, per the backend (ms), or
 *  null when the snapshot doesn't say (older backend, runtime absent). */
export function liveTurnLastActiveMs(
  response: LiveTurnStatusResponse | null | undefined,
  runtimeId: string,
  storedSessionId?: null | string
): null | number {
  const lastActive = liveTurnRow(response, runtimeId, storedSessionId)?.row.last_active
  const ms = Number(lastActive) * 1000

  return lastActive != null && Number.isFinite(ms) && ms > 0 ? ms : null
}

/** One desktop.log line per silent-turn check: which row (if any) the verdict
 *  was read from. A wrong "ended" left no trace before this. */
export function liveTurnCheckLine(
  response: LiveTurnStatusResponse | null | undefined,
  runtimeId: string,
  storedSessionId: null | string | undefined,
  nowMs: number
): string {
  const rows = Array.isArray(response?.sessions) ? String(response.sessions.length) : '-'
  const found = liveTurnRow(response, runtimeId, storedSessionId)
  const head = `[silent-turn] runtime=${runtimeId} stored=${storedSessionId || '-'} verdict=${liveTurnVerdict(response, runtimeId, storedSessionId)} rows=${rows}`

  if (!found) {
    return `${head} match=none`
  }

  const lastActiveMs = liveTurnLastActiveMs(response, runtimeId, storedSessionId)
  const idle = lastActiveMs === null ? '-' : String(Math.max(0, Math.round((nowMs - lastActiveMs) / 1000)))

  return `${head} match=${found.by} row=${found.row.id ?? '-'} status=${found.row.status ?? '-'} idle_s=${idle}`
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
