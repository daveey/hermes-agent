import { describe, expect, it } from 'vitest'

import { sessionStatusStripeColor } from './session-status-stripe'

describe('sessionStatusStripeColor', () => {
  it('external is sky-400 (dark sky-500); external_down is red-500', () => {
    expect(sessionStatusStripeColor('external')).toBe(
      '[--session-status-color:var(--color-sky-400)] dark:[--session-status-color:var(--color-sky-500)]'
    )
    expect(sessionStatusStripeColor('external_down')).toBe('[--session-status-color:var(--color-red-500)]')
  })
})
