import { JsonRpcGatewayError } from '@hermes/shared'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { renderMessageStream } from '@/app/session/hooks/use-message-stream/test-harness'
import type { ClientSessionState } from '@/app/types'
import { type ChatMessage, textPart, toChatMessages } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import { clearSessionDraft, mainComposerScope, takeSessionDraft } from '@/store/composer'
import { resetRefusedSendsForTests, withdrawRefusedSend } from '@/store/refused-sends'
import { $sessionStates } from '@/store/session-states'
import type { SessionMessage } from '@/types/hermes'

import { DRAFT_PERSIST_DEBOUNCE_MS, type QueueEditState } from '../composer-utils'
import { composerPlainText, RICH_INPUT_SLOT } from '../rich-editor'

import { useComposerDraft } from './use-composer-draft'
import { useComposerSubmit } from './use-composer-submit'

// A send to a busy chat that still can't be queued keeps the user's words: either the submit is
// refused before admission (SESSION_OWNER_BUSY, the composer restores the draft), or the owner
// admitted it and the turn was then refused because another writer held the lease
// (failure_reason session_busy: an immediate serve refusal, the gateway's lease-wait timeout, a
// taken-over chat). The draft and submit hooks, the stash and the message stream are the real ones;
// only the assistant-ui adapter is substituted.
const runtime = vi.hoisted(() => {
  let text = ''
  const listeners = new Set<() => void>()

  const setText = vi.fn((next: string) => {
    text = next
    listeners.forEach(listener => listener())
  })

  return {
    composer: { setText },
    getState: () => ({ text }),
    reset: () => {
      text = ''
      listeners.clear()
    },
    subscribe: (listener: () => void) => {
      listeners.add(listener)

      return () => listeners.delete(listener)
    }
  }
})

vi.mock('@assistant-ui/react', () => ({
  useAui: () => ({ composer: () => runtime.composer }),
  useAuiState: (selector: (state: { composer: { text: string } }) => unknown) =>
    selector({ composer: runtime.getState() }),
  useComposerRuntime: () => runtime
}))

const SCOPE = 'busy-refusal-session'
const RUNTIME = 'busy-refusal-runtime'
const TEXT = 'please summarize the open PRs'
const queueEditRef = { current: null as QueueEditState | null }

type Submit = (text: string, target?: { submission_id?: string }) => Promise<boolean>

let submitDraft: () => void
let onSubmit: Submit

function Harness() {
  const draft = useComposerDraft({
    activeQueueSessionKey: SCOPE,
    focusKey: null,
    inputDisabled: false,
    queueEditRef,
    sessionId: RUNTIME
  })

  const submit = useComposerSubmit({
    ...draft,
    activeQueueSessionKey: SCOPE,
    attachments: [],
    busy: false,
    disabled: false,
    drainNextQueued: async () => false,
    exitQueuedEdit: () => false,
    inputDisabled: false,
    onCancel: () => {},
    onSteer: async () => true,
    onSteerHidden: async () => true,
    onSubmit: (text, target) => onSubmit(text, target),
    queueCurrentDraft: () => false,
    queueEdit: null,
    queuedPrompts: [],
    sessionId: RUNTIME
  })

  submitDraft = submit.submitDraft

  return (
    <div
      contentEditable
      data-slot={RICH_INPUT_SLOT}
      data-testid="editor"
      ref={draft.editorRef}
      suppressContentEditableWarning
    />
  )
}

function typeAndSave(editor: HTMLElement, text: string) {
  editor.textContent = text
  editor.dispatchEvent(new Event('input', { bubbles: true }))
  act(() => {
    runtime.composer.setText(text)
  })
  act(() => void vi.advanceTimersByTime(DRAFT_PERSIST_DEBOUNCE_MS + 50))
}

const composerText = (editor: HTMLElement) => composerPlainText(editor).trim()

const userBubble = (rowId?: number): ChatMessage => ({
  id: 'user-optimistic',
  role: 'user',
  parts: [textPart(TEXT)],
  ...(rowId !== undefined ? { rowId } : {})
})

const publish = (state: ClientSessionState) => act(() => $sessionStates.set({ [RUNTIME]: state }))

/** Send TEXT from the composer; the owner admits it (the composer empties). Returns its id. */
async function sendAdmitted(editor: HTMLElement): Promise<string | undefined> {
  let submissionId: string | undefined

  onSubmit = async (_text, target) => {
    submissionId = target?.submission_id

    return true
  }

  typeAndSave(editor, TEXT)
  act(() => submitDraft())
  await act(async () => undefined)
  expect(composerText(editor)).toBe('')

  return submissionId
}

/** The live stream's view of the admitted turn ending in `payload` (a message.complete error). */
function streamTurnFailure(payload: Record<string, unknown>, rowId?: number): ClientSessionState {
  const states = new Map([[RUNTIME, createClientSessionState(SCOPE, [userBubble(rowId)])]])
  const stream = renderMessageStream(RUNTIME, { states })

  act(() => stream.handleEvent({ payload: {}, session_id: RUNTIME, type: 'message.start' }))
  act(() =>
    stream.handleEvent({ payload: { status: 'error', ...payload }, session_id: RUNTIME, type: 'message.complete' })
  )

  return stream.state(RUNTIME)
}

// agent/turn_facade_lease.py `_lease_not_acquired_result`, as tui_gateway/prompt_turn.py frames it.
const SERVE_REFUSAL = {
  text: 'This chat has another active writer. Use session.take_over (Take over here), then retry your message. Your message was not processed.',
  error: `session_turn_lease_timeout:${SCOPE}`,
  failure_reason: 'session_busy',
  error_surface: { layer: 'gateway', code: 'SESSION_NOT_OWNED', retryable: false },
  recoverable: true
}

const LEASE_WAIT_TIMEOUT = {
  text: '⏳ Another Hermes process kept this session busy too long. Your message was not processed - wait for the other process to finish, then send it again.',
  error: `session_turn_lease_timeout:${SCOPE}`,
  recoverable: true
}

beforeEach(() => {
  vi.useFakeTimers()
  runtime.reset()
  resetRefusedSendsForTests()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  mainComposerScope.clear()
  clearSessionDraft(SCOPE)
  $sessionStates.set({})
  resetRefusedSendsForTests()
})

describe('a busy-chat send that cannot be queued keeps the user text', () => {
  it('SESSION_OWNER_BUSY: the refused submit leaves the words in the composer', async () => {
    const { getByTestId } = render(<Harness />)
    const editor = getByTestId('editor')

    onSubmit = async () => {
      throw new JsonRpcGatewayError('The gateway is running a turn in this chat and could not queue it.', {
        code: 4090,
        data: { reason: 'SESSION_OWNER_BUSY' }
      })
    }

    typeAndSave(editor, TEXT)
    act(() => submitDraft())
    await act(async () => undefined)

    expect(composerText(editor)).toBe(TEXT)
    expect(takeSessionDraft(SCOPE).text).toBe(TEXT)
  })

  it('SESSION_OWNER_BUSY answered as a rejected send (accepted === false) keeps the words too', async () => {
    const { getByTestId } = render(<Harness />)
    const editor = getByTestId('editor')

    onSubmit = async () => false
    typeAndSave(editor, TEXT)
    act(() => submitDraft())
    await act(async () => undefined)

    expect(composerText(editor)).toBe(TEXT)
  })

  it('a serve turn refused at once by a held lease gives the words back to the composer', async () => {
    const { getByTestId } = render(<Harness />)
    const editor = getByTestId('editor')

    await sendAdmitted(editor)
    publish(streamTurnFailure(SERVE_REFUSAL, 41))

    expect(composerText(editor)).toBe(TEXT)
    expect(takeSessionDraft(SCOPE).text).toBe(TEXT)
  })

  it('a gateway turn that waited out the lease gives the words back (older frame: error prefix only)', async () => {
    const { getByTestId } = render(<Harness />)
    const editor = getByTestId('editor')

    await sendAdmitted(editor)
    publish(streamTurnFailure(LEASE_WAIT_TIMEOUT))

    expect(composerText(editor)).toBe(TEXT)
  })

  it('a turn refused because another window took the chat over gives the words back', async () => {
    const { getByTestId } = render(<Harness />)
    const editor = getByTestId('editor')

    await sendAdmitted(editor)
    publish(
      streamTurnFailure({
        text: 'This chat was taken over by another Hermes window. Resume it explicitly to continue here. Your message was not processed.',
        failure_reason: 'session_busy',
        error_surface: { layer: 'gateway', code: 'SESSION_NOT_OWNED', retryable: false }
      })
    )

    expect(composerText(editor)).toBe(TEXT)
  })

  it('a failed_turn row with failure_reason session_busy brings the text back on a reopened chat', () => {
    const rows: SessionMessage[] = [
      { id: 7, role: 'user', content: TEXT, timestamp: 1 } as SessionMessage,
      {
        role: 'assistant',
        content: 'This turn did not complete.',
        timestamp: 2,
        display_kind: 'failed_turn',
        display_metadata: { error: `session_turn_lease_timeout:${SCOPE}`, failure_reason: 'session_busy' }
      } as SessionMessage
    ]

    const { getByTestId } = render(<Harness />)
    publish(createClientSessionState(SCOPE, toChatMessages(rows)))

    expect(composerText(getByTestId('editor'))).toBe(TEXT)
  })

  it('a failed_turn row whose error surface names session_busy brings the text back', () => {
    const rows: SessionMessage[] = [
      { id: 8, role: 'user', content: TEXT, timestamp: 1 } as SessionMessage,
      {
        role: 'assistant',
        content: 'This turn did not complete.',
        timestamp: 2,
        display_kind: 'failed_turn',
        display_metadata: { error_surface: { layer: 'gateway', code: 'session_busy', retryable: true } }
      } as SessionMessage
    ]

    const { getByTestId } = render(<Harness />)
    publish(createClientSessionState(SCOPE, toChatMessages(rows)))

    expect(composerText(getByTestId('editor'))).toBe(TEXT)
  })

  it('another failed turn (a provider error) leaves the composer alone', async () => {
    const { getByTestId } = render(<Harness />)
    const editor = getByTestId('editor')

    await sendAdmitted(editor)
    publish(
      streamTurnFailure({
        text: 'rate limited',
        error: 'rate limited',
        error_surface: { layer: 'provider', code: 'rate_limit', retryable: true }
      })
    )

    expect(composerText(editor)).toBe('')
  })

  it('keeps what the user typed meanwhile and adds the refused words after it', async () => {
    const { getByTestId } = render(<Harness />)
    const editor = getByTestId('editor')

    await sendAdmitted(editor)
    typeAndSave(editor, 'and the next thing')
    publish(streamTurnFailure(SERVE_REFUSAL, 41))

    expect(composerPlainText(editor)).toBe(`and the next thing\n\n${TEXT}`)
  })
})

describe('nothing runs twice', () => {
  it('offers the words once: a re-read transcript does not refill a composer the user emptied', async () => {
    const { getByTestId } = render(<Harness />)
    const editor = getByTestId('editor')

    await sendAdmitted(editor)
    const refused = streamTurnFailure(SERVE_REFUSAL, 41)
    publish(refused)
    expect(composerText(editor)).toBe(TEXT)

    typeAndSave(editor, '')
    publish({ ...refused, messages: [...refused.messages] })

    expect(composerText(editor)).toBe('')
  })

  it('the given-back words are a new message, not a retry of the settled submission id', async () => {
    const { getByTestId } = render(<Harness />)
    const editor = getByTestId('editor')

    const first = await sendAdmitted(editor)
    publish(streamTurnFailure(SERVE_REFUSAL, 41))
    expect(composerText(editor)).toBe(TEXT)

    let resent: string | undefined

    onSubmit = async (_text, target) => {
      resent = target?.submission_id

      return true
    }

    act(() => submitDraft())
    await act(async () => undefined)

    expect(first).toBeTruthy()
    expect(resent).toBeTruthy()
    expect(resent).not.toBe(first)
  })

  it('re-running the refused turn from its card takes the untouched offer back', async () => {
    const { getByTestId } = render(<Harness />)
    const editor = getByTestId('editor')

    await sendAdmitted(editor)
    publish(streamTurnFailure(SERVE_REFUSAL, 41))
    expect(composerText(editor)).toBe(TEXT)

    act(() => withdrawRefusedSend(SCOPE, TEXT))

    expect(composerText(editor)).toBe('')
    expect(takeSessionDraft(SCOPE).text).toBe('')
  })

  it('an edited offer is the user’s new message: a re-run of the card leaves it', async () => {
    const { getByTestId } = render(<Harness />)
    const editor = getByTestId('editor')

    await sendAdmitted(editor)
    publish(streamTurnFailure(SERVE_REFUSAL, 41))
    typeAndSave(editor, `${TEXT} and the issues`)

    act(() => withdrawRefusedSend(SCOPE, TEXT))

    expect(composerText(editor)).toBe(`${TEXT} and the issues`)
  })

  it('the live refusal, then the same turn hydrated with its row id, is still one offer', async () => {
    const { getByTestId } = render(<Harness />)
    const editor = getByTestId('editor')

    await sendAdmitted(editor)
    publish(streamTurnFailure(SERVE_REFUSAL))
    expect(composerText(editor)).toBe(TEXT)
    typeAndSave(editor, '')

    const rows: SessionMessage[] = [
      { id: 41, role: 'user', content: TEXT, timestamp: 1 } as SessionMessage,
      {
        role: 'assistant',
        content: SERVE_REFUSAL.text,
        timestamp: 2,
        display_kind: 'failed_turn',
        display_metadata: { failure_reason: 'session_busy' }
      } as SessionMessage
    ]

    publish(createClientSessionState(SCOPE, toChatMessages(rows)))

    expect(composerText(editor)).toBe('')
  })

  it('the same words sent again and refused again come back again', async () => {
    const { getByTestId } = render(<Harness />)
    const editor = getByTestId('editor')

    await sendAdmitted(editor)
    const first = streamTurnFailure(SERVE_REFUSAL, 41)
    publish(first)
    expect(composerText(editor)).toBe(TEXT)

    // Enter on the given-back words: a new pending user turn sits at the tail.
    act(() => submitDraft())
    await act(async () => undefined)
    expect(composerText(editor)).toBe('')
    publish({ ...first, messages: [...first.messages, { ...userBubble(), id: 'user-again', pending: true }] })

    // ...and it is refused too.
    publish({
      ...first,
      messages: [
        ...first.messages,
        { ...userBubble(), id: 'user-again' },
        { id: 'refusal-again', role: 'assistant', parts: [], error: 'busy', refusedSend: true }
      ]
    })

    expect(composerText(editor)).toBe(TEXT)
  })

  it('a later user turn means the refusal is history: nothing is offered', () => {
    const later: ChatMessage = { id: 'user-later', role: 'user', parts: [textPart('something else')] }
    const refused = streamTurnFailure(SERVE_REFUSAL, 41)

    const { getByTestId } = render(<Harness />)
    publish({ ...refused, messages: [...refused.messages, later] })

    expect(composerText(getByTestId('editor'))).toBe('')
  })
})
