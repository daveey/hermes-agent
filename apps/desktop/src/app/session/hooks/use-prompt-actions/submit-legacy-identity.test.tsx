import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getLatestSessionMessages } from '@/hermes'

import { actRender, Harness, type HarnessHandle } from './index-test-utils'
import { clearStoredJournal } from './prepared-submissions-test-utils'
import { clearSingleFlightSessionResumeState } from './single-flight-resume'
import { resetSubmissionIdSupportForTests } from './submission-identity-support'

vi.mock('@/hermes', () => ({
  getApiRequestConnection: () => null,
  getApiRequestProfile: () => null,
  getLatestSessionMessages: vi.fn(async () => ({ messages: [], session_id: 'session' })),
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  getSession: vi.fn(),
  PROMPT_SUBMIT_REQUEST_TIMEOUT_MS: 1_800_000,
  setApiRequestProfile: vi.fn(),
  transcribeAudio: vi.fn()
}))

vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  requestGatewayForAgent: vi.fn()
}))

beforeEach(() => {
  clearSingleFlightSessionResumeState()
  clearStoredJournal()
  resetSubmissionIdSupportForTests()
  vi.mocked(getLatestSessionMessages).mockImplementation(async () => ({ messages: [], session_id: 'session' }))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  resetSubmissionIdSupportForTests()
})

describe('a backend that refuses submission_id', () => {
  it('a legacy backend refusing submission_id as version skew (4000) gets one identityless retry', async () => {
    // `hermes serve` validates params against a strict contract: an unknown key is refused
    // as 4000 before any handler ran (surfaced by the remote-topology E2E as "Session unavailable").
    const submits: Array<Record<string, unknown>> = []

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method !== 'prompt.submit') {return {} as never}
      submits.push(params!)

      if ('submission_id' in params!) {
        throw Object.assign(new Error('invalid params for prompt.submit: submission_id: Extra inputs are not permitted — the client and the Hermes backend are out of sync (different versions)'), { code: 4000 })
      }

      return { status: 'streaming' } as never
    })

    let handle: HarnessHandle | null = null
    await actRender(
      <Harness onReady={h => (handle = h)} rawAdmissionReceipts refreshSessions={async () => undefined} requestGateway={requestGateway} />
    )
    expect(await handle!.submitText('hi remote')).toBe(true)
    expect(submits).toHaveLength(2)
    expect(submits[1]).not.toHaveProperty('submission_id')
    expect(submits[1]).toMatchObject({ text: 'hi remote' })
  })

  it('remembers the refusal: the next send on that backend goes identityless in ONE request', async () => {
    const submits: Array<Record<string, unknown>> = []

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method !== 'prompt.submit') {return {} as never}
      submits.push(params!)

      if ('submission_id' in params!) {
        throw Object.assign(new Error('invalid params for prompt.submit: submission_id: Extra inputs are not permitted — the client and the Hermes backend are out of sync (different versions)'), { code: 4000 })
      }

      return { status: 'streaming' } as never
    })

    let handle: HarnessHandle | null = null
    await actRender(
      <Harness onReady={h => (handle = h)} rawAdmissionReceipts refreshSessions={async () => undefined} requestGateway={requestGateway} />
    )

    expect(await handle!.submitText('first')).toBe(true)
    expect(submits).toHaveLength(2)

    expect(await handle!.submitText('second')).toBe(true)
    expect(submits).toHaveLength(3)
    expect(submits[2]).not.toHaveProperty('submission_id')
    expect(submits[2]).toMatchObject({ text: 'second' })
  })

  it('an explicit retry of an uncertain IDENTIFIED send keeps its identity even after a refusal was remembered', async () => {
    // The first send may have been admitted with identity (an ambiguous failure, not a refusal).
    // Retrying it identityless would bypass the backend's dedupe and could admit the turn twice.
    const submits: Array<Record<string, unknown>> = []
    let phase: 'uncertain' | 'legacy' | 'retry' = 'uncertain'

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method !== 'prompt.submit') {return {} as never}
      submits.push(params!)

      if (phase === 'uncertain') {throw new Error('socket closed before acknowledgement')}

      if (phase === 'legacy' && 'submission_id' in params!) {
        throw Object.assign(new Error('invalid params for prompt.submit: submission_id: Extra inputs are not permitted'), { code: 4000 })
      }

      return { admission_id: params?.submission_id, status: 'started' } as never
    })

    let handle: HarnessHandle | null = null
    await actRender(
      <Harness onReady={h => (handle = h)} rawAdmissionReceipts refreshSessions={async () => undefined} requestGateway={requestGateway} />
    )

    expect(await handle!.submitText('maybe admitted', { submission_id: 'uncertain-1' })).toBe(false)
    phase = 'legacy'
    expect(await handle!.submitText('unrelated new turn')).toBe(true)
    expect(submits.at(-1)).not.toHaveProperty('submission_id')

    phase = 'retry'
    const before = submits.length
    expect(await handle!.submitText('maybe admitted', { submission_id: 'uncertain-1' })).toBe(true)
    expect(submits[before]).toMatchObject({ submission_id: 'uncertain-1', text: 'maybe admitted' })
  })
})
