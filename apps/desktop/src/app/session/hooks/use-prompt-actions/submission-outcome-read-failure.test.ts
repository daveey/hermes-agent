import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as journal from './prepared-submissions'
import type { PreparedSubmission } from './prepared-submissions'
import { clearStoredJournal, storedJournal } from './prepared-submissions-test-utils'
import { submissionAborted, submissionFailed, writeInFlightSubmission } from './submission-outcome'

vi.mock('./prepared-submissions', async importOriginal => {
  const actual = await importOriginal<typeof journal>()

  return { ...actual, readPreparedSubmission: vi.fn(actual.readPreparedSubmission) }
})

const entry = (): PreparedSubmission => ({ id: 'sub-1', owner: null as never, attachments: [], text: 'hello', params: {} })
const definite = () => true

beforeEach(() => clearStoredJournal())

describe('the in-flight write when the journal cannot be read', () => {
  it('keeps an earlier unknown attempt from the retained entry it holds, so a later abort stays unknown', async () => {
    const first = entry()
    await writeInFlightSubmission('k', first)
    await submissionFailed('k', first, new Error('request timed out'), { definite: () => false, replayUncertain: false })

    // The retry holds the retained entry as adopted; reading the journal again now fails.
    const retry = { ...storedJournal().k }
    vi.mocked(journal.readPreparedSubmission).mockRejectedValueOnce(new Error('EIO: journal unreadable'))
    await writeInFlightSubmission('k', retry)
    await submissionAborted('k', retry, () => false)

    expect(storedJournal().k.outcome).toBe('unknown')
  })

  it('a fresh send (nothing retained) is not made unknown by the failed read', async () => {
    const fresh = entry()
    vi.mocked(journal.readPreparedSubmission).mockRejectedValueOnce(new Error('EIO: journal unreadable'))
    await writeInFlightSubmission('k', fresh)
    await submissionFailed('k', fresh, new Error('refused'), { definite, replayUncertain: false })

    expect(storedJournal().k.outcome).toBe('refused')
  })
})
