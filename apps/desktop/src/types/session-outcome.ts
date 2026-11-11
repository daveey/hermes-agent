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

/** Historical scoped claims, not current execution status or permission to run remaining work. */
export interface CompletionReceipt {
  version: 1
  items: {
    scope: string
    disposition: 'delivered' | 'superseded' | 'contained' | 'rejected'
    evidence: { ref: string; observed_at: string; verification: 'reported' | 'independently_checked' }[]
    remaining: { scope: string; state: 'pending' | 'deferred'; owner: string | null; reason: string }[]
    superseded_by?: string
  }[]
}

/** Source revision/namespace is projected unchanged, including future additive provenance fields. */
export type CompletionReceiptSource = Record<string, unknown>

/** [daveey] A chat's stored outcome, as list rows carry it (`row.outcome`). */
export interface SessionOutcome {
  kind: SessionOutcomeKind | string
  source?: 'declared' | 'derived'
  /** Epoch seconds. */
  at?: number
  summary?: string
  items?: SessionOutcomeItem[]
  on?: SessionOutcomeWaitingOn[]
  /** Recurring jobs a `done` chat owns: they never end, so they are named, not waited on. */
  crons?: SessionOutcomeWaitingOn[]
  reason?: SessionOutcomeReason | string
  /** Last admitted receipt; can remain historical while kind describes new work. Absence is unspecified. */
  completion_receipt?: CompletionReceipt
  completion_receipt_source?: CompletionReceiptSource
}
