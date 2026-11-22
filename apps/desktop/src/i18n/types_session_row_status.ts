/** [daveey] Sidebar row status copy: kanban badge, background-process badge, and the status-stripe tooltips for a
 *  row's stored outcome (session-outcome contract). Part of `Translations['sidebar']['row']`. */
export interface SessionRowStatusCopy {
  cardColours: { red: string; pink: string; amber: string; blue: string; grey: string; green: string; violet: string }
  kanbanTasks: (open: number, blocked: number) => string
  kanbanAttention: (count: number) => string
  kanbanStalled: (count: number) => string
  kanbanUnknown: (count: number) => string
  outcomeDone: string
  outcomeCrons: (names: string[]) => string
  /** [daveey] Cards a done chat still watches; `label` = card title, `eta` = wake time in epoch seconds, when known. */
  outcomeWatches: (cards: { id: string; label?: string; eta?: number }[]) => string
  outcomeDecision: string
  outcomeMuted: string
  outcomeRunning: string
  outcomeWaiting: string
  /** [daveey] The red is a queue card a restart may have lost (this app's queue, not the backend's status). */
  pausedQueue: string
  outcomeUnresolved: (reason: null | string) => string
  /** [daveey] An unread dot whose stored outcome is not done: unread, then what the outcome says. */
  unreadNotDone: (outcome: string) => string
  muteFailed: string
  backgroundProcesses: (count: number) => string
  /** [daveey] The hollow background dot when its only open work has unknown or unfresh status (never "running"). */
  backgroundUnknown: string
  /** [daveey] The hollow background dot when its only open work is known waiting (parked, held or a fresh wait). */
  backgroundWaiting: string
  /** [daveey] Background dot when the chat's open work is running on an external service (CI/deploy). */
  backgroundExternal: string
  /** [daveey] Background dot when the only open external work is down. */
  backgroundExternalDown: string
  kanbanExternalRunning: string
  kanbanExternalDown: string
  backgroundProcessUnnamed: string
  stopProcess: string
  stoppingProcess: string
}
