import { describe, expect, it } from 'vitest'

import { mapUpdateProgress, type UpdateProgressResponse } from './background-update'

const FETCHED = 1_000_000

const wire = (over: Partial<NonNullable<UpdateProgressResponse['progress']>> = {}): UpdateProgressResponse => ({
  progress: {
    state: 'draining',
    due_at: 1,
    state_since: 1,
    elapsed_s: 30,
    state_elapsed_s: 30,
    cap_s: 60,
    waiting_on: [],
    ...over
  }
})

describe('mapUpdateProgress', () => {
  it('keeps the four update states and drops everything unusable', () => {
    expect(mapUpdateProgress(wire(), FETCHED)).toEqual({
      state: 'draining',
      elapsedS: 30,
      stateElapsedS: 30,
      capS: 60,
      waitingOn: [],
      fetchedAt: FETCHED
    })
    expect(mapUpdateProgress({ progress: null }, FETCHED)).toBeNull()
    expect(mapUpdateProgress(undefined, FETCHED)).toBeNull()
    expect(mapUpdateProgress(wire({ state: 'idle' }), FETCHED)).toBeNull()
    expect(mapUpdateProgress(wire({ elapsed_s: Number.NaN }), FETCHED)).toBeNull()
    expect(mapUpdateProgress(wire({ cap_s: 0 }), FETCHED)?.capS).toBeNull()
    expect(mapUpdateProgress(wire({ elapsed_s: 40, state_elapsed_s: undefined }), FETCHED)?.stateElapsedS).toBe(40)
  })
})
