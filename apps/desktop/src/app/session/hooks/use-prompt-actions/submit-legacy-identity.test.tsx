import { JsonRpcGatewayError } from '@hermes/shared'
import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getLatestSessionMessages } from '@/hermes'
import { en } from '@/i18n/en'
import { $notifications, clearNotifications } from '@/store/notifications'
import { pageStorage } from '@/test/page-storage'

import { actRender, Harness, type HarnessHandle, RUNTIME_SESSION_ID } from './index-test-utils'
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

describe('a refused identityless send', () => {
  const legacySends = (submits: Array<Record<string, unknown>>) => submits.filter(p => !('submission_id' in p)).length

  // A backend whose PromptSubmitParams forbids submission_id refuses it before admission (4094),
  // so every send goes identityless; `refusal` answers the first identityless one.
  function refusingGateway(refusal: Error, submits: Array<Record<string, unknown>>) {
    return vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume') {return { session_id: RUNTIME_SESSION_ID } as never}

      if (method !== 'prompt.submit') {return {} as never}
      submits.push(params!)

      if (params?.submission_id) {throw Object.assign(new Error('capability refused'), { code: 4094 })}

      if (legacySends(submits) === 1) {throw refusal}

      return { ok: true } as never
    })
  }

  // Chat 20261112_052539_8d8534: Take over's retry of a send the owner check refused before
  // admission showed legacySendUnconfirmed, though that send never ran. A definite refusal is not
  // an unconfirmed send; an unconfirmed owner hand-off (SESSION_OWNER_UNCONFIRMED) still is.
  it.each([
    ['SESSION_NOT_OWNED', true],
    ['SESSION_OWNER_UNCONFIRMED', false]
  ])('an explicit retry after a legacy send refused 4090 %s resends it: %s', async (reason, resent) => {
    const submits: Array<Record<string, unknown>> = []
    clearNotifications()

    const refusal = new JsonRpcGatewayError('This chat is active in another Hermes window/terminal.', {
      code: 4090,
      data: { reason }
    })

    const requestGateway = refusingGateway(refusal, submits)
    let handle: HarnessHandle | null = null
    await actRender(
      <Harness onReady={h => (handle = h)} rawAdmissionReceipts refreshSessions={async () => undefined} requestGateway={requestGateway} />
    )
    expect(await handle!.submitText('take me over', { submission_id: 'refused-legacy' })).toBe(false)
    expect(legacySends(submits)).toBe(1)
    clearNotifications()
    // Take over's explicit retry names the refused send's identity.
    expect(await handle!.submitText('take me over', { submission_id: 'refused-legacy' })).toBe(resent)
    const shown = $notifications.get().map(n => n.message)

    if (resent) {
      expect(legacySends(submits)).toBe(2)
      expect(shown).not.toContain(en.promptNotices.legacySendUnconfirmed)
    } else {
      expect(legacySends(submits)).toBe(1)
      expect(shown).toContain(en.promptNotices.legacySendUnconfirmed)
    }
  })

  it('a refused legacy send whose reset cannot be saved still surfaces the 4090 refusal, not the storage error', async () => {
    const submits: Array<Record<string, unknown>> = []
    clearNotifications()

    const refusal = new JsonRpcGatewayError('This chat is active in another Hermes window/terminal.', {
      code: 4090,
      data: { reason: 'SESSION_NOT_OWNED' }
    })

    const storageError = new Error('QuotaExceededError: the journal is full')
    const storage = pageStorage()
    const setItem = storage.setItem
    let resetWrites = 0

    // Every save after the refusal fails: the reset of `legacyAttempted`, then the refusal outcome
    // (submission-outcome.ts) that is recorded on the same entry.
    vi.spyOn(storage, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (legacySends(submits) === 1 && JSON.parse(value)?.legacyAttempted === false) {
        resetWrites += JSON.parse(value).outcome === undefined ? 1 : 0
        throw storageError
      }

      return setItem.call(this, key, value)
    })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    const requestGateway = refusingGateway(refusal, submits)
    let handle: HarnessHandle | null = null
    await actRender(
      <Harness onReady={h => (handle = h)} rawAdmissionReceipts refreshSessions={async () => undefined} requestGateway={requestGateway} />
    )
    expect(await handle!.submitText('take me over', { submission_id: 'refused-unsaved' })).toBe(false)
    expect(legacySends(submits)).toBe(1)
    expect(resetWrites).toBe(1)

    const errors = handle!.state().messages.flatMap(m => (m.error ? [m.error] : []))
    expect(errors.at(-1)).toContain('active in another Hermes window')
    expect(errors.join('\n')).not.toContain('journal is full')
    expect($notifications.get().map(n => n.message).join('\n')).not.toContain('journal is full')
    expect(warn).toHaveBeenCalledWith('[prepared-submission-reset]', storageError)
    expect(warn).toHaveBeenCalledWith('[prepared-submission-outcome]', storageError)
  })

  it('a legacy send refused session busy (4009) is retried by the busy loop instead of failing as unconfirmed', async () => {
    const submits: Array<Record<string, unknown>> = []
    const requestGateway = refusingGateway(new JsonRpcGatewayError('session busy', { code: 4009 }), submits)
    let handle: HarnessHandle | null = null
    await actRender(
      <Harness onReady={h => (handle = h)} rawAdmissionReceipts refreshSessions={async () => undefined} requestGateway={requestGateway} />
    )
    expect(await handle!.submitText('wait for the turn', { submission_id: 'busy-legacy' })).toBe(true)
    expect(legacySends(submits)).toBe(2)
  })
})
