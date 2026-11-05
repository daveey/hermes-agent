/** [daveey] Sidebar row status copy: kanban badge, background-process badge, and the status-stripe tooltips for a
 *  row's stored outcome (session-outcome contract). Part of `Translations['sidebar']['row']`. */
export interface SessionRowStatusCopy {
  kanbanTasks: (open: number, blocked: number) => string
  kanbanAttention: (count: number) => string
  kanbanStalled: (count: number) => string
  outcomeDone: string
  outcomeCrons: (names: string[]) => string
  outcomeDecision: string
  outcomeMuted: string
  outcomeRunning: string
  outcomeWaiting: string
  /** [daveey] The red is a queue card a restart may have lost (this app's queue, not the backend's status). */
  pausedQueue: string
  outcomeUnresolved: (reason: null | string) => string
  muteFailed: string
  backgroundProcesses: (count: number) => string
  backgroundProcessUnnamed: string
  stopProcess: string
  stoppingProcess: string
}
