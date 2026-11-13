import { JsonRpcGatewayError } from '@hermes/shared'
import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getLatestSessionMessages } from '@/hermes'
import { en } from '@/i18n/en'
import { $notifications, clearNotifications } from '@/store/notifications'

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

const STORED_SESSION_ID = '20261113_195531_88da30'
const ROLLED_RUNTIME_ID = 'rt-after-roll'

// tui_gateway/session_registry.py `_sess_nowait`: the frame a rolled backend answers for the
// runtime id it no longer holds, before prompt.submit claims or writes anything.
const staleRuntime = () => new JsonRpcGatewayError('session not found', { code: 4001 })

// tui_gateway/rpc_dispatch.py: `hermes serve`'s strict contract refuses the unknown key.
const identityRefused = () =>
  Object.assign(
    new Error(
      'invalid params for prompt.submit: submission_id: Extra inputs are not permitted — the client and the ' +
        'Hermes backend are out of sync (different versions); run `hermes update` and restart both'
    ),
    { code: 4000 }
  )

type Submit = Record<string, unknown>

/** A hub backend that rolled: its old runtime id is gone until `session.resume` mints a new one.
 *  `identity` says whether prompt.submit accepts `submission_id` (canonical) or refuses it (sidecar).
 *  `admitted` records every submit the backend actually admitted as a turn. */
function rolledBackend(identity: 'accepted' | 'refused', staleError: () => Error = staleRuntime) {
  const submits: Submit[] = []
  const admitted: Submit[] = []
  const resumes: Submit[] = []
  let live = RUNTIME_SESSION_ID

  const requestGateway = vi.fn(async (method: string, params?: Submit) => {
    if (method === 'session.resume') {
      resumes.push(params!)
      live = ROLLED_RUNTIME_ID

      return { session_id: ROLLED_RUNTIME_ID } as never
    }

    if (method !== 'prompt.submit') {
      return {} as never
    }

    submits.push(params!)

    if (identity === 'refused' && 'submission_id' in params!) {
      throw identityRefused()
    }

    if (params!.session_id !== live) {
      throw staleError()
    }

    admitted.push(params!)

    return (
      identity === 'accepted'
        ? { admission_id: `adm-${admitted.length}`, status: 'started', submission_id: params!.submission_id }
        : { status: 'streaming' }
    ) as never
  })

  // The roll drops every in-memory runtime; only a resume mints a live one again.
  return { admitted, requestGateway, resumes, roll: () => (live = ''), submits }
}

async function render(requestGateway: ReturnType<typeof rolledBackend>['requestGateway']): Promise<HarnessHandle> {
  let handle: HarnessHandle | null = null
  await actRender(
    <Harness
      onReady={h => (handle = h)}
      rawAdmissionReceipts
      refreshSessions={async () => undefined}
      requestGateway={requestGateway}
      storedSessionId={STORED_SESSION_ID}
    />
  )

  return handle!
}

function visibleFailures(handle: HarnessHandle): string[] {
  return [
    ...handle.state().messages.flatMap(m => (m.error ? [String(m.error)] : [])),
    ...$notifications.get().map(n => n.message)
  ]
}

beforeEach(() => {
  clearSingleFlightSessionResumeState()
  clearStoredJournal()
  clearNotifications()
  resetSubmissionIdSupportForTests()
  vi.mocked(getLatestSessionMessages).mockImplementation(async () => ({ messages: [], session_id: 'session' }))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  resetSubmissionIdSupportForTests()
})

describe('a send that meets "session not found" after a hub backend roll', () => {
  // Incident 2026-11-13 12:05:43 / 12:06:37 PT: the hub's sidecar refuses submission_id, so Desktop
  // remembered the refusal and sent identityless; the rolled backend answered 4001 for the old runtime,
  // the resume re-entered the submit, and the re-entry threw "Legacy submission acknowledgement is
  // unknown" — the typed message was never sent.
  it('identityless (sidecar refused submission_id earlier): resumes and lands ONE turn, no failure shown', async () => {
    const backend = rolledBackend('refused')
    const handle = await render(backend.requestGateway)

    // Before the roll: the first send teaches Desktop that this backend refuses submission_id.
    expect(await handle.submitText('before the roll')).toBe(true)
    expect(backend.admitted).toHaveLength(1)

    backend.roll()
    clearNotifications()
    const sentBefore = backend.submits.length

    expect(await handle.submitText('typed right after the roll')).toBe(true)

    const afterRoll = backend.submits.slice(sentBefore)
    // One identityless send to the dead runtime, one to the resumed runtime; nothing identified.
    expect(afterRoll.map(s => [s.session_id, 'submission_id' in s])).toEqual([
      [RUNTIME_SESSION_ID, false],
      [ROLLED_RUNTIME_ID, false]
    ])
    expect(backend.resumes).toHaveLength(1)
    expect(backend.admitted.filter(s => s.text === 'typed right after the roll')).toHaveLength(1)
    expect(visibleFailures(handle)).toEqual([])
  })

  it('identityless on a fresh client (refusal not yet remembered): still ONE turn after the resume', async () => {
    const backend = rolledBackend('refused')
    backend.roll()
    const handle = await render(backend.requestGateway)

    expect(await handle.submitText('first send after the roll')).toBe(true)
    expect(backend.admitted).toEqual([
      expect.objectContaining({ session_id: ROLLED_RUNTIME_ID, text: 'first send after the roll' })
    ])
    expect(backend.admitted[0]).not.toHaveProperty('submission_id')
    expect(visibleFailures(handle)).toEqual([])
  })

  it('identified (backend accepts submission_id): resumes and resends the SAME identity, ONE turn', async () => {
    const backend = rolledBackend('accepted')
    backend.roll()
    const handle = await render(backend.requestGateway)

    expect(await handle.submitText('identified after the roll', { submission_id: 'sub-roll-1' })).toBe(true)
    expect(backend.submits.map(s => [s.session_id, s.submission_id])).toEqual([
      [RUNTIME_SESSION_ID, 'sub-roll-1'],
      [ROLLED_RUNTIME_ID, 'sub-roll-1']
    ])
    expect(backend.admitted).toHaveLength(1)
    expect(visibleFailures(handle)).toEqual([])
  })
})

describe('duplicate-turn protection for an identityless send is unchanged', () => {
  // Only the bare legacy 4001 frame is an answer. Anything else that merely reads "session not found"
  // (a codeless error from a wrapped rethrow, a canonical refusal with a reason) or a timeout says
  // nothing about whether the send ran, so the retry stays refused.
  it.each([
    ['a codeless "session not found"', () => new Error('session not found')],
    [
      'a 4001 carrying a canonical reason',
      () => new JsonRpcGatewayError('session not found', { code: 4001, data: { reason: 'permission_denied' } })
    ],
    [
      'a 4001 whose prose only mentions it',
      () => new JsonRpcGatewayError('tool failed: session not found in cache', { code: 4001 })
    ],
    ['a timeout', () => new Error('request timed out after 1800s: prompt.submit')]
  ])('%s keeps the identityless send unconfirmed: no automatic resend', async (_label, staleError) => {
    const backend = rolledBackend('refused', staleError)
    const handle = await render(backend.requestGateway)
    expect(await handle.submitText('before the roll')).toBe(true)

    backend.roll()
    const sentBefore = backend.submits.length
    expect(await handle.submitText('ambiguous after the roll')).toBe(false)

    const afterRoll = backend.submits.slice(sentBefore)
    expect(afterRoll).toHaveLength(1)
    expect(backend.admitted.filter(s => s.text === 'ambiguous after the roll')).toHaveLength(0)
    expect(visibleFailures(handle).join('\n')).toContain('acknowledgement is unknown')
  })

  it('an explicit retry after a stale-runtime refusal resends it once (nothing was admitted)', async () => {
    const backend = rolledBackend('refused')
    const handle = await render(backend.requestGateway)
    expect(await handle.submitText('before the roll')).toBe(true)
    backend.roll()

    // The resume itself fails once, so the first attempt surfaces the original 4001 refusal.
    const original = backend.requestGateway.getMockImplementation()!
    let failResume = true
    backend.requestGateway.mockImplementation(async (method: string, params?: Submit) => {
      if (method === 'session.resume' && failResume) {
        failResume = false
        throw new Error('backend restarting')
      }

      return original(method, params)
    })

    expect(await handle.submitText('retry me', { submission_id: 'roll-retry' })).toBe(false)
    clearNotifications()
    expect(await handle.submitText('retry me', { submission_id: 'roll-retry' })).toBe(true)
    expect(backend.admitted.filter(s => s.text === 'retry me')).toHaveLength(1)
    expect($notifications.get().map(n => n.message)).not.toContain(en.promptNotices.legacySendUnconfirmed)
  })
})
