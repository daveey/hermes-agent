import { describe, expect, it } from 'vitest'

import * as projector from '@/lib/execution-observations'
import { sameCronSignature } from '@/lib/session-signatures'
import * as sdk from '@/sdk'
import type { SessionInfo } from '@/types/hermes'

import fixture from '../../../../schemas/fixtures/execution-observations-v1.json'

describe('plugin SDK execution projection seam', () => {
  it('exports the same pure projector the sidebar consumes, not a copy', () => {
    for (const name of [
      'executionIdentityKey',
      'projectExecutionObservation',
      'selectExecutionObservation',
      'summarizeExecutionObservations'
    ] as const) {
      expect(sdk[name]).toBe(projector[name])
    }
  })

  it('classifies shared-fixture evidence for a plugin, with the caller-supplied clock', () => {
    expect(sdk.projectExecutionObservation(fixture.base, 1001).state).toBe('running')
    expect(sdk.projectExecutionObservation(fixture.base, 1180).state).toBe('unknown')
  })
})

describe('row signature', () => {
  it('republishes a list row when only its execution observations change', () => {
    const row = { id: 's', message_count: 1, source: 'cli', started_at: 0, title: 't' } as SessionInfo
    const withObs = { ...row, execution_observations: [fixture.base] }

    expect(sameCronSignature([row], [{ ...row }])).toBe(true)
    expect(sameCronSignature([row], [withObs])).toBe(false)
    expect(sameCronSignature([withObs], [{ ...withObs, execution_observations: [{ ...fixture.base }] }])).toBe(true)
  })
})
