import type { SessionStatusStripe } from '@/store/session-dot-state'

/** Shared sidebar stripe / session-tab underline palette, including dark grey. */
export const sessionStatusStripeColor = (stripe: null | SessionStatusStripe): string => {
  switch (stripe) {
    case 'done':
      return '[--session-status-color:var(--color-emerald-500)]'

    case 'stuck':
      return '[--session-status-color:var(--color-red-500)]'

    default:
      return '[--session-status-color:var(--color-zinc-400)] dark:[--session-status-color:var(--color-zinc-500)]'
  }
}
