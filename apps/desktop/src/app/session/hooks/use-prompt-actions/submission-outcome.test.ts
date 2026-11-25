import { beforeEach, describe, expect, it } from 'vitest'

import type { PreparedSubmission } from './prepared-submissions'
import { clearStoredJournal, storedJournal } from './prepared-submissions-test-utils'
import {
  noteUnansweredReplays,
  sendUnconfirmed,
  submissionAborted,
  submissionFailed,
  writeInFlightSubmission
} from './submission-outcome'

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

  it.each([
    ['an abort', (prepared: PreparedSubmission) => submissionAborted('k', prepared, () => false)],
    ['a definite refusal', (prepared: PreparedSubmission) =>
      submissionFailed('k', prepared, refused, { definite, replayUncertain: false })]
  ] as const)('%s on a retry never downgrades an earlier unknown attempt', async (_label, end) => {
    const first = entry()
    await writeInFlightSubmission('k', first)
    await submissionFailed('k', first, new Error('request timed out'), { definite, replayUncertain: false })

    const retry = { ...storedJournal().k }
    await writeInFlightSubmission('k', retry)
    await end(retry)
    expect(storedJournal().k.outcome).toBe('unknown')

    // And a later attempt still remembers it.
    const third = { ...storedJournal().k }
    await writeInFlightSubmission('k', third)
    await submissionAborted('k', third, () => false)
    expect(storedJournal().k.outcome).toBe('unknown')
  })

  it('an earlier refused or aborted attempt does not make a retry unknown', async () => {
    const first = entry()
    await writeInFlightSubmission('k', first)
    await submissionAborted('k', first, () => false)

    const retry = { ...storedJournal().k }
    await writeInFlightSubmission('k', retry)
    await submissionFailed('k', retry, refused, { definite, replayUncertain: false })
    expect(storedJournal().k.outcome).toBe('refused')
  })
})

describe('a recovery wrapper that calls the submit again', () => {
  const calls = async (errors: unknown[]) => {
    let replays = 0

    const run = noteUnansweredReplays(async (n: number) => {
      if (errors[n] !== undefined) { throw errors[n] }

      return 'ok'
    }, definite, () => { replays += 1 })

    for (let n = 0; n <= errors.length; n += 1) {
      await run(n).catch(() => undefined)
    }

    return replays
  }

  it('marks a resubmit after a timeout (maybe admitted) as a replay, once per later call', async () => {
    expect(await calls([new Error('request timed out')])).toBe(1)
    expect(await calls([new Error('request timed out'), refused])).toBe(2)
  })

  it('does not mark a resubmit after a definite refusal (nothing was admitted)', async () => {
    expect(await calls([refused])).toBe(0)
  })
})
