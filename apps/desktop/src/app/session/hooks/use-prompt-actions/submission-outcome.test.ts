import { beforeEach, describe, expect, it } from 'vitest'

import type { PreparedSubmission } from './prepared-submissions'
import { clearStoredJournal, storedJournal } from './prepared-submissions-test-utils'
import { sendUnconfirmed, submissionAborted, submissionFailed, writeInFlightSubmission } from './submission-outcome'

const entry = (outcome?: PreparedSubmission['outcome']): PreparedSubmission => ({
  id: 'sub-1',
  owner: null as never,
  attachments: [],
  text: 'hello',
  params: {},
  ...(outcome ? { outcome } : {})
})

const refused = new Error('refused')
const definite = (error: unknown) => error === refused

beforeEach(() => clearStoredJournal())

describe('the outcome a journaled send records', () => {
  it('says "could not confirm" only for an unknown or unrecorded outcome', () => {
    expect(sendUnconfirmed(undefined)).toBe(false)
    expect(sendUnconfirmed(entry())).toBe(true)
    expect(sendUnconfirmed(entry('unknown'))).toBe(true)
    expect(sendUnconfirmed(entry('refused'))).toBe(false)
    expect(sendUnconfirmed(entry('aborted'))).toBe(false)
  })

  it('a new attempt clears the earlier outcome while it is in flight', async () => {
    const prepared = entry('refused')
    await writeInFlightSubmission('k', prepared)
    expect(storedJournal().k.outcome).toBeUndefined()
  })

  it.each([
    ['a definite refusal is refused', refused, false, 'refused'],
    ['a definite refusal after an unanswered try of the same submit is unknown', refused, true, 'unknown'],
    ['any other failure is unknown', new Error('request timed out'), false, 'unknown']
  ] as const)('%s', async (_label, error, replayUncertain, outcome) => {
    const prepared = entry()
    expect(await submissionFailed('k', prepared, error, { definite, replayUncertain })).toBe(error)
    expect(storedJournal().k.outcome).toBe(outcome)
  })

  it('an abort before the submit left is aborted, and the abort still runs', async () => {
    const prepared = entry()
    expect(await submissionAborted('k', prepared, () => false)).toBe(false)
    expect(storedJournal().k.outcome).toBe('aborted')
  })
})
