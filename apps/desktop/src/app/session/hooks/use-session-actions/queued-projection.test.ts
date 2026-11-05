import { expect, it } from 'vitest'

import { queuedProjectionMessage } from './queued-projection'
import { preserveEquivalentTranscript } from './utils'

it('publishes hold metadata changes even when queued words and identity stay the same', () => {
  const plain = [queuedProjectionMessage('s', { user: 'later' })]
  const held = [queuedProjectionMessage('s', { user: 'later', queued_reason: 'core_update', queued_started_at: null })]
  const timed = [queuedProjectionMessage('s', { user: 'later', queued_reason: 'core_update', queued_started_at: 1234 })]
  expect(preserveEquivalentTranscript(plain, held)).toBe(held)
  expect(preserveEquivalentTranscript(held, timed)).toBe(timed)
  expect(preserveEquivalentTranscript(timed, plain)).toBe(plain)
})
