import type { SessionInfo } from '@/types/hermes'

/** Counts and the owner-qualified reason share the backend's frozen observation. */
export function sessionKanbanLabel(
  session: SessionInfo,
  labels: {
    kanbanTasks: (open: number, blocked: number) => string
    kanbanStalled: (count: number) => string
    kanbanAttention: (count: number) => string
  }
): string {
  const open = session.kanban_active ?? 0
  const blocked = session.kanban_blocked ?? 0
  const stalled = session.kanban_stalled ?? 0
  const attention = session.kanban_attention ?? 0

  return [
    open > 0 || blocked > 0 ? labels.kanbanTasks(open, blocked) : '',
    blocked > 0 ? session.kanban_blocked_on?.join(', ') : '',
    stalled > 0 ? labels.kanbanStalled(stalled) : '',
    attention > 0 ? labels.kanbanAttention(attention) : ''
  ]
    .filter(Boolean)
    .join('; ')
}
