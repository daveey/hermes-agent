import { describe, expect, it } from 'vitest'

import { isBotWakeTimeout } from './bot-wake-log'

describe('bot wake timeout classification', () => {
  it('recognizes hydration timeouts', () => {
    expect(isBotWakeTimeout(new Error("Timed out loading medicina's session history."))).toBe(true)
  })

  it.each([
    null,
    'Timed out loading history',
    { message: 'Timed out loading history' },
    new Error('Disconnected'),
    new Error('Other: Timed out loading history')
  ])('rejects unrelated failures: %s', error => {
    expect(isBotWakeTimeout(error)).toBe(false)
  })
})
