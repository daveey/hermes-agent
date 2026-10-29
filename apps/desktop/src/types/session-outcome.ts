/** [daveey] A chat's stored outcome as list rows carry it (session-outcome contract, `sessions.outcome_json`). */

/** [daveey] The kind of a chat's stored outcome (session-outcome contract). */
export type SessionOutcomeKind = 'decision' | 'done' | 'muted' | 'running' | 'unresolved' | 'waiting'

/** [daveey] Why an outcome is `unresolved`. */
export type SessionOutcomeReason =
  'empty' | 'interrupted' | 'no_summary' | 'notice_tail' | 'subagent_killed' | 'turn_failed'

/** [daveey] One open question a `decision` outcome waits on the user for. */
export interface SessionOutcomeItem {
  id: string
  question: string
  choices?: { label: string; value: string }[]
  card_id?: string
  state?: string
}

/** [daveey] One thing a `waiting` / `running` outcome waits on. */
export interface SessionOutcomeWaitingOn {
  kind: 'capacity' | 'card' | 'cron' | 'process' | 'subagent' | 'time' | string
  id: string
  label?: string
  state?: string
  /** Epoch seconds. */
  eta?: number
}

/** [daveey] A chat's stored outcome, as list rows carry it (`row.outcome`). */
export interface SessionOutcome {
  kind: SessionOutcomeKind | string
  source?: 'declared' | 'derived'
  /** Epoch seconds. */
  at?: number
  summary?: string
  items?: SessionOutcomeItem[]
  on?: SessionOutcomeWaitingOn[]
  reason?: SessionOutcomeReason | string
}
