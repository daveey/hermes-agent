import { JsonRpcGatewayError } from '@hermes/shared'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { getLatestSessionMessages } from '@/hermes'
import { en } from '@/i18n/en'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $notifications, clearNotifications } from '@/store/notifications'
import {
  $messages,
  _resetSessionOwnerHintsForTests,
  setActiveSessionId,
  setAwaitingResponse,
  setBusy,
  setMessages,
  setResumeFailedSessionId,
  setSelectedStoredSessionId,
  setSessions
} from '@/store/session'
import { pageStorage } from '@/test/page-storage'

import { useSessionActions } from '../use-session-actions'
import { resumeFailedToastId } from '../use-session-actions/resume-latches'

import { actRender, Harness, type HarnessHandle, RUNTIME_SESSION_ID } from './index-test-utils'
import { ENTRY_PREFIX } from './prepared-submissions'
import { clearStoredJournal, storedJournal } from './prepared-submissions-test-utils'
import { resetRejectedOptimisticSendsForTests } from './rejected-optimistic-sends'
import { clearSingleFlightSessionResumeState } from './single-flight-resume'
import { resetSubmissionIdSupportForTests } from './submission-identity-support'
import type { SubmitTextOptions } from './utils'

// Incident 2026-11-23 ~23:00 PST: the hub was starved, every `session.resume` timed out after
// 30s, and a send into a chat with no live runtime ended without a word: the text went back to
// the composer draft, and the chat's own resume retry painted the typed message back as a bubble
// from its start-of-resume snapshot. Two messages looked delivered that were never sent.
// In the app the routed resume is `resumeSession`, which swallows its own failure (it repaints,
// toasts "Resume failed" and resolves), so the send then waited out a second 30s timeout on its
// own `session.resume` before it gave up.

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getAllSessionMessages: vi.fn(),
  getLatestSessionMessages: vi.fn(),
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  getSession: vi.fn(),
  listAllProfileSessions: vi.fn(),
  setApiRequestProfile: vi.fn(),
  transcribeAudio: vi.fn()
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayAgent: vi.fn().mockResolvedValue(undefined),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))

vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  requestGatewayForAgent: vi.fn(),
  requestGatewayForProfile: vi.fn(),
  retainGatewayForAgent: vi.fn(async () => () => undefined)
}))

vi.mock('@/components/pane-shell/tree/store', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  noteActiveTreeGroup: vi.fn(),
  revealTreePane: vi.fn()
}))

const STORED_SESSION_ID = 'stored-1'
const SUBMISSION_ID = 'sub-starved-1'
const BUBBLE_ID = `user-${SUBMISSION_ID}`
const TEXT = 'typed while the hub was starved'

const STORED_MESSAGES = [
  { content: 'earlier question', role: 'user', timestamp: 1 },
  { content: 'earlier answer', role: 'assistant', timestamp: 2 }
]

type Gateway = <T>(method: string, params?: Record<string, unknown>) => Promise<T>

type ResumeSession = (
  storedSessionId: string,
  replaceRoute?: boolean,
  ownerRoute?: undefined,
  options?: { authoritativeSnapshot?: boolean }
) => Promise<unknown>

const RESUMED = {
  info: {},
  message_count: STORED_MESSAGES.length,
  messages: STORED_MESSAGES,
  resumed: STORED_SESSION_ID,
  session_id: 'runtime-1',
  session_key: STORED_SESSION_ID
}

const copy = en.desktop
const NOT_SENT_BACK_IN_COMPOSER = `${copy.promptNotSent} ${copy.promptBackInComposer}`
// What use-composer-submit passes for a draft it restores when submit returns false.
const COMPOSER_SEND = { submission_id: SUBMISSION_ID, unsent: 'restored' } as const
const resumeTimeout = () => new Error('request timed out after 30s: session.resume')

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void

  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })

  return { promise, reject, resolve }
}

/** One window surface running the real `resumeSession` for the chat. */
function ChatResume({ onReady, requestGateway }: { onReady: (resume: ResumeSession) => void; requestGateway: Gateway }) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })
  const stateMapRef = ref(new Map<string, ClientSessionState>())

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef: ref<string | null>(null),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    getRoutedStoredSessionId: () => null,
    holdSessionTranscriptView: () => () => undefined,
    navigate: vi.fn() as never,
    requestGateway,
    resetViewSync: vi.fn(),
    routedSessionId: null,
    runtimeIdByStoredSessionIdRef: ref(new Map<string, string>()),
    selectedStoredSessionId: STORED_SESSION_ID,
    selectedStoredSessionIdRef: ref<string | null>(STORED_SESSION_ID),
    sessionStateByRuntimeIdRef: stateMapRef,
    syncSessionStateToView: () => undefined,
    updateSessionState: (sessionId, updater, storedSessionId) => {
      const next = updater(stateMapRef.current.get(sessionId) ?? createClientSessionState(storedSessionId ?? null))
      stateMapRef.current.set(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    onReady(actions.resumeSession as ResumeSession)
  }, [actions.resumeSession, onReady])

  return null
}

async function mountChatResume(requestGateway: Gateway): Promise<ResumeSession> {
  let resume: null | ResumeSession = null
  render(<ChatResume onReady={r => (resume = r)} requestGateway={requestGateway} />)
  await waitFor(() => expect(resume).not.toBeNull())

  return resume!
}

const messageIds = () => $messages.get().map(message => message.id)

const resumeCalls = (gateway: { mock: { calls: unknown[][] } }) =>
  gateway.mock.calls.filter(([method]) => method === 'session.resume').length

const errorNotices = () => $notifications.get().filter(notice => notice.kind === 'error')
/** Error notices other than `resumeSession`'s own "Resume failed" toast for the chat. */
const noticesFromSend = () => errorNotices().filter(notice => notice.id !== resumeFailedToastId(STORED_SESSION_ID))

/** A foreground send into a chat whose runtime binding is gone, parked on the resume that must
 *  rebind it.
 *  `routed`: the route resume is the real `resumeSession` (as wired in the app); its
 *  `session.resume` RPC is the one that times out.
 *  `direct`: the route resume returned without publishing a runtime and without a failure, so
 *  submit's own `session.resume` RPC is the one that times out.
 *  `routed-throws`: a route resume that rejects outright (no RPC of its own in this test). */
async function sendIntoUnboundChat(path: 'direct' | 'routed' | 'routed-throws') {
  const resumeRpc = deferred<never>()
  const submits: Record<string, unknown>[] = []
  const selectedStoredSessionIdRef: MutableRefObject<string | null> = { current: STORED_SESSION_ID }
  // The first send parks on the deferred RPC; the retry meets a hub that answers again.
  let hubAnswers = false

  // The route resume's own gateway (routed path only).
  const routeGateway = vi.fn((method: string) =>
    method === 'session.resume' && !hubAnswers ? resumeRpc.promise : Promise.resolve(RESUMED as never)
  )

  const routeResume = path === 'routed' ? await mountChatResume(routeGateway as unknown as Gateway) : null

  const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'session.resume') {
      return (hubAnswers ? { session_id: 'rt-rebound' } : resumeRpc.promise) as never
    }

    if (method === 'prompt.submit') {
      submits.push(params!)
    }

    return {} as never
  })

  let prompt: HarnessHandle | null = null
  await actRender(
    <Harness
      activeSessionId={null}
      activeSessionIdRef={{ current: null }}
      getRoutedStoredSessionId={() => STORED_SESSION_ID}
      getRuntimeIdForStoredSession={() => null}
      onReady={handle => (prompt = handle)}
      refreshSessions={async () => undefined}
      requestGateway={requestGateway}
      resumeStoredSession={async id => void (await (path === 'routed-throws' ? resumeRpc.promise : routeResume?.(id, true)))}
      selectedStoredSessionIdRef={selectedStoredSessionIdRef}
      storedSessionId={STORED_SESSION_ID}
    />
  )

  let sent!: Promise<boolean>
  await act(async () => {
    // The composer names every send; `false` is its signal to restore the draft under this id.
    sent = prompt!.submitTextRaw(TEXT, COMPOSER_SEND)
  })
  await waitFor(() => expect(messageIds()).toContain(BUBBLE_ID))

  if (path !== 'routed-throws') {
    await waitFor(() => expect(resumeCalls(path === 'routed' ? routeGateway : requestGateway)).toBe(1))
  }

  return {
    failResume: () =>
      act(async () => {
        resumeRpc.reject(resumeTimeout())

        return sent
      }),
    requestGateway,
    retry: () => {
      hubAnswers = true

      return prompt!.submitText(TEXT, COMPOSER_SEND)
    },
    selectedStoredSessionIdRef,
    submits
  }
}

/** Another surface's resume of the same chat (use-route-resume's retry), started while the typed
 *  message is on screen, so its start-of-resume snapshot holds the bubble. `session.resume` is
 *  single-flight per chat:
 *  `joins` shares the RPC already in flight and fails with it;
 *  `queued` asks for an authoritative snapshot, which sends its own RPC once that one settled;
 *  `first` finds no RPC in flight and sends its own at once. */
async function startConcurrentChatResume(flight: 'first' | 'joins' | 'queued') {
  const ownRpc = deferred<typeof RESUMED>()

  const requestGateway = vi.fn((method: string) =>
    method === 'session.resume' ? ownRpc.promise : Promise.resolve({} as never)
  )

  const resume = await mountChatResume(requestGateway as unknown as Gateway)

  let settled!: Promise<unknown>
  await act(async () => {
    settled = resume(STORED_SESSION_ID, true, undefined, flight === 'queued' ? { authoritativeSnapshot: true } : undefined)
  })

  const settle = (answer: () => void) => async () => {
    if (flight !== 'joins') {
      await waitFor(() => expect(resumeCalls(requestGateway)).toBe(1))
    }

    await act(async () => {
      // A joined resume never sent an RPC of its own: it already failed with the shared one.
      if (flight !== 'joins') {
        answer()
      }

      await settled
    })
  }

  return {
    /** It times out too and repaints from the REST transcript. */
    failAndRepaint: settle(() => ownRpc.reject(resumeTimeout())),
    /** The hub answers it and it repaints from the resumed runtime. */
    succeedAndRepaint: settle(() => ownRpc.resolve(RESUMED))
  }
}

/** One send whose `session.resume` is refused at once, with the caller's options. */
async function sendWithResumeDown(options: SubmitTextOptions) {
  const requestGateway = vi.fn(async (method: string) => {
    if (method === 'session.resume') {
      throw resumeTimeout()
    }

    return {} as never
  })

  let prompt: HarnessHandle | null = null
  await actRender(
    <Harness
      activeSessionId={null}
      activeSessionIdRef={{ current: null }}
      onReady={handle => (prompt = handle)}
      refreshSessions={async () => undefined}
      requestGateway={requestGateway}
      storedSessionId={STORED_SESSION_ID}
    />
  )

  const accepted = await prompt!.submitText(TEXT, options)

  expect(resumeCalls(requestGateway)).toBe(1)

  return accepted
}

interface FirstAttempt {
  /** What the attempt's n-th prompt.submit does (it throws). */
  submit?: (n: number) => never
  /** What the attempt's session.resume answers (default: it times out). */
  resume?: () => unknown
  /** The user switches chats right after the journal entry is written: the submit never leaves. */
  driftOnJournalWrite?: boolean
}

const ownerRefusal = () =>
  new JsonRpcGatewayError('This chat is active in another Hermes window/terminal.', {
    code: 4090,
    data: { reason: 'SESSION_NOT_OWNED' }
  })

const submitTimeout = () => new Error('request timed out after 1800s: prompt.submit')

/** One chat and one composer send (same identity every time). `attempt` sends it into the chat's live
 *  runtime and lets it end as the spec says, leaving its journal entry; `retry` sends it again after the
 *  runtime binding is lost and the hub stops answering resumes, so that retry's notice must say what is
 *  known about the earlier attempts. */
async function chatWithRetainedSend() {
  const activeSessionIdRef: MutableRefObject<string | null> = { current: RUNTIME_SESSION_ID }
  const busyRef: MutableRefObject<boolean> = { current: false }
  const selectedStoredSessionIdRef: MutableRefObject<string | null> = { current: STORED_SESSION_ID }
  const runtimeIdByStoredSessionIdRef = { current: new Map([[STORED_SESSION_ID, RUNTIME_SESSION_ID]]) }
  let spec: FirstAttempt = {}
  let submits = 0

  const requestGateway = vi.fn(async (method: string) => {
    if (method === 'prompt.submit' && spec.submit) {
      submits += 1
      spec.submit(submits)
    }

    if (method === 'session.resume' && spec.resume) {
      return spec.resume() as never
    }

    throw resumeTimeout()
  })

  // The page's live storage: on Node 26 the setup's in-memory stand-in is not a Storage instance.
  const storage = pageStorage()
  const setItem = storage.setItem
  vi.spyOn(storage, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
    setItem.call(this, key, value)

    if (spec.driftOnJournalWrite && key.startsWith(ENTRY_PREFIX)) {
      selectedStoredSessionIdRef.current = 'another-chat'
    }
  })

  let prompt: HarnessHandle | null = null
  await actRender(
    <Harness
      activeSessionIdRef={activeSessionIdRef}
      busyRef={busyRef}
      onReady={handle => (prompt = handle)}
      refreshSessions={async () => undefined}
      requestGateway={requestGateway}
      runtimeIdByStoredSessionIdRef={runtimeIdByStoredSessionIdRef}
      selectedStoredSessionIdRef={selectedStoredSessionIdRef}
      storedSessionId={STORED_SESSION_ID}
    />
  )

  const backInTheChat = (bound: boolean) => {
    spec = {}
    // A drift abort leaves the view it left to clear its own busy flag.
    selectedStoredSessionIdRef.current = STORED_SESSION_ID
    busyRef.current = false
    activeSessionIdRef.current = bound ? RUNTIME_SESSION_ID : null
    runtimeIdByStoredSessionIdRef.current.clear()

    if (bound) { runtimeIdByStoredSessionIdRef.current.set(STORED_SESSION_ID, RUNTIME_SESSION_ID) }
  }

  return {
    attempt: async (next: FirstAttempt) => {
      backInTheChat(true)
      spec = next
      submits = 0
      const sentBefore = requestGateway.mock.calls.filter(([method]) => method === 'prompt.submit').length
      expect(await prompt!.submitText(TEXT, COMPOSER_SEND)).toBe(false)
      const sent = requestGateway.mock.calls.filter(([method]) => method === 'prompt.submit').length - sentBefore
      expect(sent > 0).toBe(!next.driftOnJournalWrite)
    },
    journalOutcomes: () => Object.values(storedJournal()).map(entry => entry.outcome ?? 'unknown'),
    retry: async () => {
      backInTheChat(false)
      clearNotifications()
      const resumesBefore = resumeCalls(requestGateway)
      const accepted = await prompt!.submitText(TEXT, COMPOSER_SEND)
      expect(resumeCalls(requestGateway)).toBe(resumesBefore + 1)

      return accepted
    }
  }
}

beforeEach(() => {
  clearSingleFlightSessionResumeState()
  clearStoredJournal()
  clearNotifications()
  resetRejectedOptimisticSendsForTests()
  resetSubmissionIdSupportForTests()
  setSelectedStoredSessionId(STORED_SESSION_ID)
  setMessages([
    { id: 'stored-user', role: 'user', parts: [{ type: 'text', text: 'earlier question' }] },
    { id: 'stored-assistant', role: 'assistant', parts: [{ type: 'text', text: 'earlier answer' }] }
  ])
  vi.mocked(getLatestSessionMessages).mockResolvedValue({
    messages: STORED_MESSAGES,
    session_id: STORED_SESSION_ID
  } as never)
})

afterEach(() => {
  cleanup()
  clearNotifications()
  resetRejectedOptimisticSendsForTests()
  resetSubmissionIdSupportForTests()
  setActiveSessionId(null)
  setSelectedStoredSessionId(null)
  setResumeFailedSessionId(null)
  setBusy(false)
  setAwaitingResponse(false)
  setMessages([])
  setSessions([])
  _resetSessionOwnerHintsForTests({ storage: true })
  vi.restoreAllMocks()
})

describe('a send whose session.resume times out', () => {
  it.each(['routed', 'direct'] as const)(
    '%s resume: says the message was not sent, and a failed-resume repaint does not show it as a bubble',
    async path => {
      const send = await sendIntoUnboundChat(path)
      const chat = await startConcurrentChatResume(path === 'direct' ? 'joins' : 'queued')
      clearNotifications()

      // `false` is what makes the composer put the words back into the draft under SUBMISSION_ID.
      expect(await send.failResume()).toBe(false)

      // Exactly one notice from the send, beside resumeSession's own "Resume failed" toast.
      expect(noticesFromSend()).toHaveLength(1)
      expect(noticesFromSend()[0]).toMatchObject({ title: NOT_SENT_BACK_IN_COMPOSER })
      expect(noticesFromSend()[0]?.message).toContain('timed out')
      expect(send.submits).toEqual([])
      expect(messageIds()).not.toContain(BUBBLE_ID)
      // One timeout, not two: a failed route resume is not followed by submit's own resume.
      expect(resumeCalls(send.requestGateway)).toBe(path === 'routed' ? 0 : 1)

      await chat.failAndRepaint()

      expect(getLatestSessionMessages).toHaveBeenCalled()
      expect(JSON.stringify($messages.get())).toContain('earlier answer')
      expect(messageIds()).not.toContain(BUBBLE_ID)
      expect(JSON.stringify($messages.get())).not.toContain(TEXT)

      // Enter on the restored draft retries the same identity once the hub answers again.
      expect(await send.retry()).toBe(true)
      expect(send.submits).toEqual([expect.objectContaining({ submission_id: SUBMISSION_ID, text: TEXT })])
    }
  )

  it('a resume that succeeds after the send was rejected does not show it as a bubble either', async () => {
    const send = await sendIntoUnboundChat('routed')
    const chat = await startConcurrentChatResume('queued')

    expect(await send.failResume()).toBe(false)
    expect(messageIds()).not.toContain(BUBBLE_ID)

    await chat.succeedAndRepaint()

    expect(JSON.stringify($messages.get())).toContain('earlier answer')
    expect(messageIds()).not.toContain(BUBBLE_ID)
    expect(JSON.stringify($messages.get())).not.toContain(TEXT)
  })

  it('the transcript painted while a later resume is still pending does not show it as a bubble', async () => {
    const send = await sendIntoUnboundChat('routed-throws')
    // This resume's REST transcript arrives only after the send was rejected, and its own
    // `session.resume` never answers in this test.
    const restTranscript = deferred<{ messages: typeof STORED_MESSAGES; session_id: string }>()
    vi.mocked(getLatestSessionMessages).mockReturnValue(restTranscript.promise as never)
    await startConcurrentChatResume('first')
    clearNotifications()

    expect(await send.failResume()).toBe(false)
    expect(errorNotices().map(notice => notice.title)).toEqual([NOT_SENT_BACK_IN_COMPOSER])
    expect(messageIds()).not.toContain(BUBBLE_ID)

    await act(async () => {
      restTranscript.resolve({
        messages: [...STORED_MESSAGES, { content: 'answer only the transcript has', role: 'assistant', timestamp: 3 }],
        session_id: STORED_SESSION_ID
      })
    })

    await waitFor(() => expect(JSON.stringify($messages.get())).toContain('answer only the transcript has'))
    expect(messageIds()).not.toContain(BUBBLE_ID)
    expect(JSON.stringify($messages.get())).not.toContain(TEXT)
  })

  it.each(['routed', 'direct'] as const)(
    '%s resume: a session switch during the resume still aborts without a notification',
    async path => {
      const send = await sendIntoUnboundChat(path)
      clearNotifications()

      // The user opened another chat while the resume was pending.
      send.selectedStoredSessionIdRef.current = 'stored-other'

      expect(await send.failResume()).toBe(false)

      expect(noticesFromSend()).toEqual([])
      expect(send.submits).toEqual([])
    }
  )
})

describe('the notice says where the words are', () => {
  const queued = { fromQueue: true, sessionId: null, storedSessionId: STORED_SESSION_ID } as const

  it.each<[string, SubmitTextOptions, string[]]>([
    ['a composer send is back in the draft', { unsent: 'restored' }, [NOT_SENT_BACK_IN_COMPOSER]],
    // Callers that restore nothing: the pet overlay and the voice conversation pass no options,
    // the quick-entry window only `onAccepted`, and an external request is re-queued by its caller.
    ['a send nobody restores names no place', {}, [copy.promptNotSent]],
    // use-composer-voice: both clear the draft and restore nothing.
    ['a voice turn names no place', { voiceTurn: true }, [copy.promptNotSent]],
    ['a live delegation names no place', { surface: 'voice-live', voiceContext: 'user: hi' }, [copy.promptNotSent]],
    // use-composer-queue queueCurrentDraft: the draft was never cleared.
    [
      'a busy Enter the server queue did not take is still in the composer',
      { ...queued, unsent: 'kept' },
      [`${copy.promptNotQueued} ${copy.promptStillInComposer}`]
    ],
    ['a drain of a queued entry stays quiet: the entry stays in its queue', queued, []],
    ['a hidden send is machine text', { displayKind: 'hidden' }, [copy.promptFailed]]
  ])('%s', async (_label, options, titles) => {
    expect(await sendWithResumeDown({ submission_id: SUBMISSION_ID, ...options })).toBe(false)

    expect(errorNotices().map(notice => notice.title)).toEqual(titles)
  })

  const unconfirmed = `${copy.promptSendUnconfirmed} ${copy.promptBackInComposer}`

  it.each<[string, FirstAttempt[], string, string]>([
    // The first attempt reaches the backend and its answer never comes back.
    ['is unknown: the retry cannot say it was not sent', [{ submit: () => { throw submitTimeout() } }],
      'unknown', unconfirmed],
    // The owner answered that it admitted nothing (another window holds the chat).
    ['was a definite refusal: the retry says it was not sent', [{ submit: () => { throw ownerRefusal() } }],
      'refused', NOT_SENT_BACK_IN_COMPOSER],
    // The user switched chats after the journal entry was written and before the submit left.
    ['was aborted before the send: the retry says it was not sent', [{ driftOnJournalWrite: true }],
      'aborted', NOT_SENT_BACK_IN_COMPOSER],
    // Attempt 1 may have been admitted; attempt 2 aborting before it sent cannot make that "not sent".
    ['was unknown, then a retry aborted before sending: still unconfirmed',
      [{ submit: () => { throw submitTimeout() } }, { driftOnJournalWrite: true }], 'unknown', unconfirmed],
    // Nor can a later definite refusal of a retry: the owner may hold attempt 1 under another state.
    ['was unknown, then a retry was refused: still unconfirmed',
      [{ submit: () => { throw submitTimeout() } }, { submit: () => { throw ownerRefusal() } }], 'unknown', unconfirmed],
    // Inside one attempt: the submit timed out (maybe admitted), the timeout recovery resumed the chat
    // and resubmitted, and that resubmit was refused. The first submit is still unaccounted for.
    ['timed out, was resubmitted by the timeout recovery and then refused: unconfirmed',
      [{ submit: n => { throw n === 1 ? submitTimeout() : ownerRefusal() }, resume: () => ({ session_id: 'rt-recovered' }) }],
      'unknown', unconfirmed]
  ])('a retry of a send whose earlier attempt %s', async (_label, attempts, outcome, notice) => {
    const chat = await chatWithRetainedSend()

    for (const spec of attempts) {
      await chat.attempt(spec)
    }

    expect(chat.journalOutcomes()).toEqual([outcome])
    expect(await chat.retry()).toBe(false)
    expect(errorNotices().map(n => n.title)).toEqual([notice])
  })
})
