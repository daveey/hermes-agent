// ── Session row activity (fork) ─────────────────────────────────
// The [daveey] fields the backend stamps on a SessionInfo row (kanban, goal, cron, background
// work, review waits, outcome, mute, automated), moved out of ./hermes.ts unchanged: SessionInfo
// extends SessionActivityFields and ./hermes.ts re-exports this module. [daveey]

import type { SessionOutcome } from '@/types/session-outcome'

/** [daveey] One live background process a session row owns (backend stamp). */
export interface SessionBackgroundProcess {
  /** Process-registry id (`proc_…`), what Stop names. */
  id: string
  /** Redacted, whitespace-collapsed, truncated command line. */
  command: string
  /** Epoch seconds. */
  started_at?: number
  pid?: number
}

export interface SessionActivityFields {
  /** Per-card display clocks: cap six, running first, oldest first within each state. */
  kanban_timed_cards?: {
    id: string
    title: string
    state: 'running' | 'blocked' | 'waiting'
    since: number
    wait_kind?: 'ci' | 'clock' | 'deploy'
  }[]
  /** Timed cards beyond the wire cap; absent when there are no clocks. */
  kanban_timed_more?: number
  /** [daveey] Kanban tasks this conversation created (`kanban_create`) that
   *  are still queued or running, summed over its compression lineage. The
   *  turn that created them has ended, but the session's work goes on in the
   *  workers, so the sidebar paints it like a background job, not "done".
   *  Absent when zero or against a backend predating the field. */
  kanban_active?: number

  /** [daveey] The backend could not read this conversation's kanban cards (a
   *  local board or another gateway's hermes-hub replica failed). Unknown is
   *  not "no work": the row stays off done until a read succeeds. Absent when
   *  the read worked. */
  kanban_unknown?: boolean
  /** Origin projection observation lower bound (epoch seconds), including known zero work. */
  kanban_observed_at?: number
  /** Optional v1 execution observations (source/identity/timing/evidence; see lib/execution-observations.ts).
   *  Additive: absent or empty means "no evidence" and the legacy stamps above decide alone. Capacity diagnostics
   *  are separate fields, never work state. Entries are untrusted JSON until projected. */
  execution_observations?: unknown[]
  /** [daveey] How many of `kanban_active`/`kanban_blocked` are cards on another
   *  gateway known only from a stale hermes-hub replica (last-known status).
   *  Absent when zero. */
  kanban_remote_stale?: number
  /** [daveey] Open card ids (queued/running + waiting on a human) the chat waits on, over the lineage. */
  kanban_waiting_on?: string[]
  /** [daveey] Kanban tasks of this conversation (created or subscribed) that
   *  are blocked on a human, summed over its compression lineage. Shown next
   *  to the open count on the sidebar row. Absent when zero. */
  kanban_blocked?: number
  /** Owner-qualified blocked card keys, from the same snapshot as kanban_blocked (capped). */
  kanban_blocked_on?: string[]
  /** [daveey] The open human asks (blocked/triage cards with a validated human_request), from the same snapshot as
   *  `kanban_blocked`. Present only when non-empty, then its length is the authoritative ask count; absent = fall back to `kanban_blocked`. */
  needs_you?: { kind: string; question: string; ref: string }[]
  /** [daveey] How many of `kanban_active` nobody is working: scheduled on a timed/event wake, blocked on a
   *  non-human kind, or todo behind an unfinished parent. Stamped (even 0) whenever `kanban_active` is, so its
   *  absence means a backend that predates it. */
  kanban_parked?: number
  /** [daveey] How many of `kanban_active` are queued behind a card blocked on a human ask (painted red). Absent
   *  when zero. */
  kanban_held?: number
  /** Backend-authored overdue triage attention; never a human request. */
  kanban_triage_attention?: { count: number; refs: string[]; parked: number; held: number; stalled: number }
  /** [daveey] How many of `kanban_parked` wait on something only an agent can end (transient/capability block,
   *  triage, continuation exhausted, a park with no named producer) and have waited over 2 h. Painted amber
   *  "stalled", distinct from grey waiting and red needs-you. Absent when zero. */
  kanban_stalled?: number
  /** [daveey] Parked cards in state external_running OR external_unknown (subset of kanban_parked). Absent → today. */
  kanban_external?: number
  /** [daveey] Subset of kanban_external whose last poll is unknown. */
  kanban_external_unknown?: number
  /** [daveey] Parked cards in state external_down. Not added to kanban_blocked. */
  kanban_external_down?: number
  /** [daveey] Cap 10, newest first: cards behind kanban_external* so the sidebar stripe can name each service. */
  kanban_external_cards?: {
    /** ETA readout of a CI / deploy / timer wait (`kanban_wait_info.eta_fields`). Absent on other cards. */
    eta?: {
      estimate_source?: null | string
      expected_s?: null | number
      wait_kind?: null | string
      waited_since?: null | number
    } | null
    external?: {
      adapter?: string
      health: string
      job: string
      next_due_at?: null | number
      observed_at?: null | number
      reason?: string
      service: string
      url?: null | string
    } | null
    task_id: string
    title?: string
  }[]
  /** [daveey] Finished kanban cards (created or subscribed) whose result this
   *  conversation has not been handed — it was not loaded in any backend when
   *  they completed. Paints the row green (unread) until the gateway's
   *  unloaded-session sweeper writes the notice into the transcript. Absent
   *  when zero. */
  kanban_unseen?: number
  /** [daveey] Done kanban cards (last 24 h) whose handoff admits dropped,
   *  deferred or unrun work with no follow-up card, per a plugin-registered
   *  check. Paints the row amber ("needs attention") instead of green. Absent
   *  when zero. */
  kanban_attention?: number
  /** [daveey] True while this conversation's standing `/goal` is still
   *  active (running between turns or parked on a judge `wait` timer). The
   *  goal loop resumes it unasked, so the sidebar paints it like a background
   *  job, not "done". Absent otherwise or against an older backend. */
  goal_active?: boolean
  /** [daveey] True while this conversation (over its lineage) owns an enabled,
   *  scheduled cron job: it does its work periodically, so the sidebar shows a
   *  loop glyph and treats the row like a parked (grey) one instead of "done".
   *  Absent otherwise or against an older backend. */
  cron_active?: boolean
  /** [daveey] Live background processes (terminal background=true) this
   *  conversation owns, per the backend's process registry and the shared
   *  processes.json, summed over its compression lineage. Keeps the sidebar
   *  row off "done" even when this window never polled the session. Absent
   *  when zero or against an older backend. */
  background_active?: number
  /** [daveey] What those processes are (oldest first, capped): the sidebar's
   *  process badge lists each command and its age, with a Stop button. */
  background_processes?: SessionBackgroundProcess[]
  turn_live?: boolean // [daveey] a live turn lease on the lineage, any process (hermes_cli/turn_session_activity.py)
  /** [daveey] Set when this conversation's latest reply links an open GitHub
   *  PR still waiting on a code review: `stuck` when the review is on the
   *  viewer (or nobody / others asked >24h ago), `parked` when only other
   *  reviewers were requested in the last 24h. Absent otherwise. */
  review_wait?: 'parked' | 'stuck'
  /** [daveey] The PR `review_wait` refers to. */
  review_wait_pr?: string
  /** [daveey] Stored outcome (sessions.outcome_json); wins over derived status. Absent on older backends. */
  outcome?: null | SessionOutcome
  /** [daveey] ⌘B park, persisted (sessions.muted). Absent on older backends: the local list rules. */
  muted?: boolean
  /** [daveey] The owning profile's `sessions.automated_sources` matched this
   *  row (bot / smoke-test traffic). Hidden from the sidebar unless "Show
   *  automated & test sessions" is on; see store/sidebar-automated.ts. */
  automated?: boolean
}
