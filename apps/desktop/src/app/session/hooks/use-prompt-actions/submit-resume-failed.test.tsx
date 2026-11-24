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

import { useSessionActions } from '../use-session-actions'
import { resumeFailedToastId } from '../use-session-actions/resume-latches'

import { actRender, Harness, type HarnessHandle } from './index-test-utils'
import { clearStoredJournal } from './prepared-submissions-test-utils'
import { resetRejectedOptimisticSendsForTests } from './rejected-optimistic-sends'
import { clearSingleFlightSessionResumeState } from './single-flight-resume'
import { resetSubmissionIdSupportForTests } from './submission-identity-support'

// Incident 2026-11-23 ~23:00 PST: the hub was starved, every `session.resume` timed out after
// 30s, and a send into a chat with no live runtime ended without a word: the text went back to
// the composer draft, and the chat's own resume retry painted the typed message back as a bubble
// from its start-of-resume snapshot. Two messages looked delivered that were never sent.

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
type ResumeSession = (storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>

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

/** The chat's own resume (use-route-resume's retry): the real `resumeSession`, whose failed-resume
 *  repaint carries the local pending turn it saw when it started. */
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
    onReady(actions.resumeSession)
  }, [actions.resumeSession, onReady])

  return null
}

const messageIds = () => $messages.get().map(message => message.id)

const resumeCalls = (gateway: { mock: { calls: unknown[][] } }) =>
  gateway.mock.calls.filter(([method]) => method === 'session.resume').length

const errorNotices = () => $notifications.get().filter(notice => notice.kind === 'error')

/** A foreground send into a chat whose runtime binding is gone, parked on the resume that must
 *  rebind it. `routed` parks on the high-level route resume (`resumeStoredSession`); `direct` lets
 *  that one return without a runtime and parks on submit's own `session.resume` RPC. */
async function sendIntoUnboundChat(path: 'direct' | 'routed') {
  const routedResume = deferred<void>()
  const directResume = deferred<{ session_id: string }>()
  const submits: Record<string, unknown>[] = []
  const selectedStoredSessionIdRef: MutableRefObject<string | null> = { current: STORED_SESSION_ID }
  // The first send parks on the deferred resumes; the retry meets a hub that answers again.
  let hubAnswers = false

  const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'session.resume') {
      return (hubAnswers ? { session_id: 'rt-rebound' } : directResume.promise) as never
    }

    if (method === 'prompt.submit') {
      submits.push(params!)
    }

    return {} as never
  })

  const resumeStoredSession = vi.fn(async () => (hubAnswers ? undefined : routedResume.promise))

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
      resumeStoredSession={resumeStoredSession}
      selectedStoredSessionIdRef={selectedStoredSessionIdRef}
      storedSessionId={STORED_SESSION_ID}
    />
  )

  let sent!: Promise<boolean>
  await act(async () => {
    // The composer names every send; `false` is its signal to restore the draft under this id.
    sent = prompt!.submitTextRaw(TEXT, { submission_id: SUBMISSION_ID })
  })
  await waitFor(() => expect(resumeStoredSession).toHaveBeenCalledWith(STORED_SESSION_ID))
  expect(messageIds()).toContain(BUBBLE_ID)

  if (path === 'direct') {
    await act(async () => routedResume.resolve())
    await waitFor(() => expect(resumeCalls(requestGateway)).toBe(1))
  }

  const failResume = () =>
    act(async () => {
      ;(path === 'routed' ? routedResume : directResume).reject(resumeTimeout())

      return sent
    })

  const retry = () => {
    hubAnswers = true

    return prompt!.submitText(TEXT, { submission_id: SUBMISSION_ID })
  }

  return { failResume, requestGateway, retry, selectedStoredSessionIdRef, submits }
}

/** Starts the chat's own resume while the bubble is on screen; its RPC never answers. */
async function startChatResume() {
  const chatResumeRpc = deferred<never>()

  const requestGateway = vi.fn((method: string) =>
    method === 'session.resume' ? chatResumeRpc.promise : Promise.resolve({} as never)
  ) as unknown as Gateway & ReturnType<typeof vi.fn>

  let resume: null | ResumeSession = null
  render(<ChatResume onReady={r => (resume = r)} requestGateway={requestGateway} />)
  await waitFor(() => expect(resume).not.toBeNull())

  let settled!: Promise<unknown>
  await act(async () => {
    settled = resume!(STORED_SESSION_ID, true)
  })

  return {
    /** The chat's resume fails too and repaints from the REST transcript. `session.resume` is
     *  single-flight per chat: on the routed path this resume sent its own RPC, which times out
     *  here; on the direct path it joined submit's RPC and failed with it. */
    failAndRepaint: () =>
      act(async () => {
        if (resumeCalls(requestGateway) > 0) {
          chatResumeRpc.reject(resumeTimeout())
        }

        await settled
      })
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
      // Snapshots the transcript while the typed message is still an optimistic bubble.
      const chat = await startChatResume()
      clearNotifications()

      // `false` is what makes the composer put the words back into the draft under SUBMISSION_ID.
      expect(await send.failResume()).toBe(false)

      // Exactly one notice from the send. (On the direct path the chat's resume shared the
      // failed RPC, so its own "Resume failed" toast may already be up beside it.)
      const fromSend = errorNotices().filter(notice => notice.id !== resumeFailedToastId(STORED_SESSION_ID))
      expect(fromSend).toHaveLength(1)
      expect(fromSend[0]).toMatchObject({ title: en.desktop.promptNotSent })
      expect(fromSend[0]?.message).toContain('timed out')
      expect(send.submits).toEqual([])
      expect(messageIds()).not.toContain(BUBBLE_ID)

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

  it.each(['routed', 'direct'] as const)(
    '%s resume: a session switch during the resume still aborts without a notification',
    async path => {
      const send = await sendIntoUnboundChat(path)

      // The user opened another chat while the resume was pending.
      send.selectedStoredSessionIdRef.current = 'stored-other'

      expect(await send.failResume()).toBe(false)

      expect($notifications.get()).toEqual([])
      expect(send.submits).toEqual([])
    }
  )
})
