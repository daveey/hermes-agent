import { describe, expect, it } from 'vitest'

import { backgroundDotLabel, sessionStatusStripe } from '@/store/session-dot-state'
import { canonicalOutcome } from '@/test/canonical-outcome'

import { sessionStatusStripeColor } from './session-status-stripe'

describe('sessionStatusStripeColor', () => {
  it.each(['ci', 'deploy'])('paints a replica %s wait light blue, including canonical status', () => {
    const row = { kanban_active: 1, kanban_parked: 1, kanban_external: 1, kanban_external_unknown: 1 }
    expect(backgroundDotLabel(row)).toBe('external')
    const outcome = canonicalOutcome('external')
    const stripe = sessionStatusStripe('background', true, false, 0, false, null, 0, 1, 0, outcome)
    expect(stripe).toBe('external')
    expect(sessionStatusStripeColor(stripe)).toContain('--color-sky-400')
    expect(sessionStatusStripeColor(stripe)).not.toContain('zinc')
    expect(sessionStatusStripeColor(stripe)).not.toContain('amber')
  })
  it('external is sky-400 (dark sky-500); external_down is red-500', () => {
    expect(sessionStatusStripeColor('external')).toBe(
      '[--session-status-color:var(--color-sky-400)] dark:[--session-status-color:var(--color-sky-500)]'
    )
    expect(sessionStatusStripeColor('external_down')).toBe('[--session-status-color:var(--color-red-500)]')
  })
})
