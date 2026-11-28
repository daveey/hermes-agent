import { JsonRpcGatewayError } from '@hermes/shared'
import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, renderHook } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { useSessionTileActions } from '@/app/chat/session-tile-actions'
import type { ClientSessionState } from '@/app/types'
import { createClientSessionState, toRuntimeMessage } from '@/lib/chat-runtime'
import { createComposerAttachmentScope } from '@/store/composer'
import { $queuedPromptsBySession, getQueuedPrompts } from '@/store/composer-queue'
import { readPendingSubmissions } from '@/store/pending-submissions'
import { $connection, $sessions } from '@/store/session'
import { $sessionStates, type SessionTileDelegate, setSessionTileDelegate } from '@/store/session-states'

import { useMessageStream } from '../use-message-stream'

import { actRender, Harness, type HarnessHandle, RUNTIME_SESSION_ID } from './index-test-utils'

vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  retainGatewayForSessionTurn: vi.fn(async () => () => undefined)
}))

beforeEach(() => {
  localStorage.clear()
  $queuedPromptsBySession.set({})
  $sessions.set([])
  $sessionStates.set({})
  $connection.set(null)
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it.each([
  ['primary', false],
  ['tile', false],
  ['primary', true],
  ['tile', true]
] as const)('%s submit preserves update metadata (legacy: %s) and idle Stop retains it', async (surface, legacy) => {
  const request = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method !== 'prompt.submit') {
      return {} as never
    }

    if (legacy && params?.submission_id) {
      throw new JsonRpcGatewayError("unknown field 'submission_id'", { code: 4000 })
    }

    return {
      status: 'queued',
      ...(!legacy ? { submission_id: params?.submission_id, admission_id: 'held-admission' } : {}),
      session_id: params?.session_id,
      queued_reason: 'core_update',
      queued_started_at: 1234
    } as never
  })

  let submit: HarnessHandle['submitText']
  let cancel: () => Promise<void>
  let state: () => ReturnType<typeof createClientSessionState>

  if (surface === 'primary') {
    let handle: HarnessHandle
    await actRender(
      <Harness
        onReady={h => {
          handle = h
        }}
        rawAdmissionReceipts
        refreshSessions={async () => undefined}
        requestGateway={request}
        storedSessionId="held-chat"
      />
    )
    submit = (text, options) => handle.submitText(text, options)
    cancel = () => handle.cancelRun()
    state = () => handle.state()
  } else {
    $sessionStates.set({ 'held-runtime': createClientSessionState('held-chat') })
    setSessionTileDelegate({
      updateSession: (id, update) => {
        const next = update($sessionStates.get()[id])
        $sessionStates.set({ ...$sessionStates.get(), [id]: next })

        return next
      }
    } as SessionTileDelegate)

    const { result } = renderHook(() =>
      useSessionTileActions({
        requestGateway: request,
        runtimeId: 'held-runtime',
        storedSessionId: 'held-chat',
        scope: {
          $awaitingInput: atom(false),
          $messages: atom([]),
          attachments: createComposerAttachmentScope(),
          target: 'tile:held-chat'
        }
      })
    )

    submit = (text, options) => result.current.submitText(text, options)
    cancel = () => result.current.cancelRun('esc')
    state = () => $sessionStates.get()['held-runtime']
  }

  await act(async () => {
    expect(await submit('after update', { submission_id: 'held-input' })).toBe(true)
  })

  if (!legacy) {
    expect(readPendingSubmissions('held-chat')['held-admission']).toMatchObject({
      queued_reason: 'core_update',
      queued_started_at: 1234
    })
    expect(getQueuedPrompts('held-chat')).toMatchObject([
      { id: 'held-admission', queued_reason: 'core_update', queued_started_at: 1234 }
    ])
  } else {
    expect(state().messages.filter(message => message.role === 'user')).toMatchObject([
      { queued_reason: 'core_update', queued_started_at: 1234 }
    ])
  }

  expect(state().busy).toBe(false)
  expect(state().messages.filter(message => message.role === 'user')).toHaveLength(legacy ? 1 : 0)
  await act(async () => {
    await cancel()
  })

  if (legacy) {
    expect(state().messages.filter(message => message.role === 'user')).toMatchObject([
      { queued_reason: 'core_update' }
    ])
  } else {
    expect(getQueuedPrompts('held-chat')).toHaveLength(1)
    expect(getQueuedPrompts('held-chat')[0].queued_reason).toBe('core_update')
  }

  expect(request.mock.calls.filter(([method]) => method === 'prompt.submit')).toHaveLength(legacy ? 2 : 1)
})

// ── Held turn delivery: drive the real gateway-event pipeline after Stop / merged holds ──

type Gate = { resolve: (value: unknown) => void }

interface HeldSurface {
  submit: (text: string, submissionId: string) => Promise<boolean>
  cancel: () => Promise<void>
  deliver: (type: string, payload?: Record<string, unknown>) => void
  state: () => ReturnType<typeof createClientSessionState>
}

/** A backend that holds idle sends for a core update (optionally pre-`submission_id`).
 *  `gates` parks each prompt.submit reply until the test releases it. */
function heldBackend(legacy: boolean, gates?: Gate[]) {
  return vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method !== 'prompt.submit') {
      return {} as never
    }

    if (legacy && params?.submission_id) {
      throw new JsonRpcGatewayError("unknown field 'submission_id'", { code: 4000 })
    }

    const reply = {
      status: 'queued',
      session_id: params?.session_id,
      queued_reason: 'core_update',
      queued_started_at: 1234,
      ...(!legacy
        ? { submission_id: params?.submission_id, admission_id: `held-${String(params?.submission_id)}` }
        : {})
    }

    if (!gates) {
      return reply as never
    }

    return (await new Promise<unknown>(resolve => gates.push({ resolve: () => resolve(reply) }))) as never
  })
}

async function mountHeld(surface: 'primary' | 'tile', request: ReturnType<typeof heldBackend>): Promise<HeldSurface> {
  if (surface === 'primary') {
    let handle!: HarnessHandle
    await actRender(
      <Harness
        onReady={h => {
          handle = h
        }}
        rawAdmissionReceipts
        refreshSessions={async () => undefined}
        requestGateway={request}
        storedSessionId="held-chat"
      />
    )

    return {
      submit: (text, submission_id) => handle.submitTextRaw(text, { submission_id }),
      cancel: () => handle.cancelRun(),
      deliver: (type, payload = {}) => handle.handleEvent({ type, session_id: RUNTIME_SESSION_ID, payload } as never),
      state: () => handle.state()
    }
  }

  const runtimeId = 'held-runtime'
  const mirror = new Map<string, ClientSessionState>()

  const updateSession = (id: string, update: (state: ClientSessionState) => ClientSessionState) => {
    const next = update($sessionStates.get()[id])
    $sessionStates.set({ ...$sessionStates.get(), [id]: next })
    mirror.set(id, next)

    return next
  }

  $sessionStates.set({ [runtimeId]: createClientSessionState('held-chat') })
  mirror.set(runtimeId, $sessionStates.get()[runtimeId])
  setSessionTileDelegate({ updateSession } as SessionTileDelegate)

  const { result } = renderHook(() => ({
    tile: useSessionTileActions({
      requestGateway: request,
      runtimeId,
      storedSessionId: 'held-chat',
      scope: {
        $awaitingInput: atom(false),
        $messages: atom([]),
        attachments: createComposerAttachmentScope(),
        target: 'tile:held-chat'
      }
    }),
    stream: useMessageStream({
      activeSessionIdRef: { current: null },
      hydrateFromStoredSession: async () => undefined,
      queryClient: new QueryClient(),
      refreshHermesConfig: async () => undefined,
      refreshSessions: async () => undefined,
      sessionStateByRuntimeIdRef: { current: mirror },
      updateSessionState: (id, update) => updateSession(id, update)
    })
  }))

  return {
    submit: (text, submission_id) => result.current.tile.submitText(text, { submission_id }),
    cancel: () =>
      act(async () => {
        await result.current.tile.cancelRun('esc')
      }),
    deliver: (type, payload = {}) =>
      act(() => {
        result.current.stream.handleGatewayEvent({ type, session_id: runtimeId, payload } as never)
      }),
    state: () => $sessionStates.get()[runtimeId]
  }
}

const replyText = (state: ClientSessionState) =>
  state.messages
    .filter(message => message.role === 'assistant')
    .flatMap(message => message.parts.flatMap(part => (part.type === 'text' ? [part.text] : [])))
    .join('')

const runHeldTurn = (view: HeldSurface) => {
  view.deliver('message.start')
  view.deliver('message.delta', { text: 'held answer' })
  view.deliver('message.complete', { text: 'held answer' })
}

const surfaces = [
  ['primary', false],
  ['tile', false],
  ['primary', true],
  ['tile', true]
] as const

it.each(surfaces)(
  '%s Stop while the held submit is in flight does not drop the held turn (legacy: %s)',
  async (surface, legacy) => {
    const gates: Gate[] = []
    const view = await mountHeld(surface, heldBackend(legacy, gates))
    let sent!: Promise<boolean>

    await act(async () => {
      sent = view.submit('held prompt', 'held-in-flight')
      await vi.waitFor(() => expect(gates).toHaveLength(1))
    })
    await view.cancel()
    expect(view.state().interrupted).toBe(true)
    await act(async () => {
      gates[0].resolve(undefined)
      expect(await sent).toBe(true)
    })

    expect(view.state()).toMatchObject({ busy: false, interrupted: false })
    runHeldTurn(view)
    expect(replyText(view.state())).toBe('held answer')
  }
)

it.each(surfaces)(
  '%s Stop after the held reply is a no-op for the later held turn (legacy: %s)',
  async (surface, legacy) => {
    const view = await mountHeld(surface, heldBackend(legacy))

    await act(async () => {
      expect(await view.submit('held prompt', 'held-after')).toBe(true)
    })
    await view.cancel()
    expect(view.state().interrupted).toBe(false)
    runHeldTurn(view)
    expect(replyText(view.state())).toBe('held answer')

    // Ordinary Stop of a running turn still latches and rejects its late stream.
    view.deliver('message.start')
    view.deliver('message.delta', { text: 'running' })
    await view.cancel()
    expect(view.state().interrupted).toBe(true)
    view.deliver('message.delta', { text: ' late straggler' })
    expect(replyText(view.state())).not.toContain('late straggler')
  }
)

it.each(['primary', 'tile'] as const)('%s legacy merged held sends all start with the merged turn', async surface => {
  const view = await mountHeld(surface, heldBackend(true))

  await act(async () => {
    expect(await view.submit('A', 'held-a')).toBe(true)
  })
  await act(async () => {
    expect(await view.submit('B', 'held-b')).toBe(true)
  })

  const held = () => view.state().messages.filter(message => message.role === 'user')

  expect(held()).toMatchObject([{ queued_reason: 'core_update' }, { queued_reason: 'core_update' }])
  view.deliver('message.start')
  expect(held().map(message => message.queuedTurnStarted)).toEqual([true, true])
  // No waiting label survives on either bubble once the merged turn runs.
  expect(held().map(message => toRuntimeMessage(message).metadata.custom?.queued_reason)).toEqual([null, null])
})
