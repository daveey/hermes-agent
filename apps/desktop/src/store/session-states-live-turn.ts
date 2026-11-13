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
