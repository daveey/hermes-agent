import type { SessionStatusStripe } from '@/store/session-dot-state'

/** Shared sidebar stripe / session-tab underline palette, including dark grey. */
export const sessionStatusStripeColor = (stripe: null | SessionStatusStripe): string => {
  switch (stripe) {
    case 'done':
      return '[--session-status-color:var(--color-emerald-500)]'

    case 'stuck':
      return '[--session-status-color:var(--color-red-500)]'

    case 'external_down':
      return '[--session-status-color:var(--color-red-500)]'

    case 'external':
      return '[--session-status-color:var(--color-sky-400)] dark:[--session-status-color:var(--color-sky-500)]'

    // [daveey] Looks done, but a finished kanban card admits dropped work.
    case 'attention':
      return '[--session-status-color:var(--color-amber-500)]'

    // [daveey] Parked kanban work that nothing is producing for over 2 h (agent-owned wait).
    case 'stalled':
      return '[--session-status-color:var(--color-amber-500)]'

    default:
      return '[--session-status-color:var(--color-zinc-400)] dark:[--session-status-color:var(--color-zinc-500)]'
  }
}
