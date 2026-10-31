// The gateway `error` event (tui_gateway/prompt_turn.py) carries only a
// message — no error_surface. The dispatcher must still classify the two
// refusals it can mean so the card and toast read like a classified turn:
// plain words up front, raw text demoted to the detail line, and Retry gone
// where retrying reproduces the failure.
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $notifications } from '@/store/notifications'
import { requestDesktopOnboarding } from '@/store/onboarding'

import { handleStatusEvent } from './status'
import type { GatewayEventContext } from './types'

vi.mock('@/store/native-notifications', () => ({ dispatchNativeNotification: vi.fn() }))
vi.mock('@/store/onboarding', () => ({ requestDesktopOnboarding: vi.fn() }))

const OWNED_REFUSAL =
  'Session 20260909_095312_6b93f5 already has a live owner (tui, pid 32977, lease age 22m). ' +
  'Attach through a compatible owner, or close the session in its owning surface before resuming here.'

function errorContext(message: string, code?: string) {
  const failAssistantMessage = vi.fn()
  const payload = { code, message } as GatewayEventContext['payload']

  const ctx: GatewayEventContext = {
    deps: {
      compactedTurnRef: { current: new Set<string>() },
      failAssistantMessage,
      flushQueuedDeltas: vi.fn(),
      hydrateFromStoredSession: vi.fn(),
      queryClient: { invalidateQueries: vi.fn() },
      sessionStateByRuntimeIdRef: { current: new Map() },
      updateSessionState: vi.fn()
    } as unknown as GatewayEventContext['deps'],
    event: { payload, session_id: 'sess-1', type: 'error' },
    explicitSid: 'sess-1',
    fromActiveSource: () => true,
    isActiveEvent: false,
    occurredAt: 1_700_000_100,
    payload,
    scheduleConfigRefresh: vi.fn(),
    sessionId: 'sess-1'
  }

  return { ctx, failAssistantMessage }
}

afterEach(() => {
  $notifications.set([])
  vi.mocked(requestDesktopOnboarding).mockClear()
})

describe('gateway `error` event → error card + toast', () => {
  it("uses the gateway's own error_surface when it sends one (taken over by another window)", () => {
    const { ctx, failAssistantMessage } = errorContext('This chat was taken over by another Hermes window.')

    const payload = {
      error_surface: { code: 'SESSION_NOT_OWNED', layer: 'gateway', retryable: false },
      message: 'This chat was taken over by another Hermes window.'
    } as unknown as GatewayEventContext['payload']

    expect(handleStatusEvent({ ...ctx, payload })).toBe(true)

    expect(failAssistantMessage).toHaveBeenCalledWith(
      'sess-1',
      'This chat was taken over by another Hermes window.',
      1_700_000_100,
      expect.objectContaining({ code: 'SESSION_NOT_OWNED', layer: 'gateway', retryable: false })
    )
  })

  it("carries the taker's machine on a takeover error so the card can name it", () => {
    const message =
      'This chat was taken over by another Hermes window.\nDetails: session s1 opened by desktop on MacBook-Pro-177 0m ago.'

    const { ctx, failAssistantMessage } = errorContext(message)

    const payload = {
      error_surface: { code: 'SESSION_NOT_OWNED', layer: 'gateway', retryable: false },
      message
    } as unknown as GatewayEventContext['payload']

    handleStatusEvent({ ...ctx, payload })

    expect(failAssistantMessage).toHaveBeenCalledWith(
      'sess-1',
      message,
      1_700_000_100,
      expect.objectContaining({
        code: 'SESSION_NOT_OWNED',
        owner: { age: '0m', host: 'MacBook-Pro-177', surface: 'desktop' }
      })
    )
  })

  it('stamps SESSION_NOT_OWNED on a live-owner refusal so the card drops Retry', () => {
    const { ctx, failAssistantMessage } = errorContext(OWNED_REFUSAL)

    expect(handleStatusEvent(ctx)).toBe(true)

    expect(failAssistantMessage).toHaveBeenCalledWith(
      'sess-1',
      OWNED_REFUSAL,
      1_700_000_100,
      expect.objectContaining({ code: 'SESSION_NOT_OWNED', retryable: false })
    )
  })

  it('toasts the plain explanation, not the lease jargon, and keeps the raw text as detail', () => {
    const { ctx } = errorContext(OWNED_REFUSAL)

    handleStatusEvent(ctx)

    const toast = $notifications.get()[0]

    expect(toast.message).toBeTruthy()
    expect(toast.message).not.toMatch(/lease|pid|live owner/i)
    expect(toast.detail).toBe(OWNED_REFUSAL)
    expect(toast.action).toBeUndefined()
  })

  it('routes a blank install to onboarding on the gateway code, whatever the sentence says', () => {
    // agent init with no usable provider: the gateway stamps code=provider_not_configured
    // (tui_gateway/server.py). The fix for this chat is setup, so the user must land there and
    // not on a toast — even after the sentence is reworded again.
    const { ctx } = errorContext(
      'Hermes could not start the assistant. Details: something new we never matched.',
      'provider_not_configured'
    )

    handleStatusEvent({ ...ctx, isActiveEvent: true })

    expect(requestDesktopOnboarding).toHaveBeenCalledTimes(1)
  })

  it('preserves exhaustion metadata and never opens recovery for background sessions or owners', () => {
    const { ctx } = errorContext('Capacity unavailable', 'provider_not_configured')
    const payload = { ...ctx.payload, failure_kind: 'exhausted' as const, provider: 'anthropic', reset_at: 1800000000 }
    handleStatusEvent({ ...ctx, payload })
    handleStatusEvent({ ...ctx, payload, isActiveEvent: true, fromActiveSource: () => false })
    expect(requestDesktopOnboarding).not.toHaveBeenCalled()
    handleStatusEvent({ ...ctx, payload, isActiveEvent: true })
    expect(requestDesktopOnboarding).toHaveBeenCalledWith('Capacity unavailable', payload)
  })

  it('does not route an unrelated failure to onboarding', () => {
    const { ctx } = errorContext('Hermes could not finish this turn. Try again.')

    handleStatusEvent(ctx)

    expect(requestDesktopOnboarding).not.toHaveBeenCalled()
  })

  it("keeps the server's own plain copy as the toast message when no code was recovered", () => {
    // tui_gateway/user_messages.py already writes actionable sentences for
    // pre-turn failures; the generic "couldn't finish" gloss must not bury them.
    const serverCopy = 'Hermes could not start the assistant for this chat. Check your model settings and try again.'
    const { ctx, failAssistantMessage } = errorContext(serverCopy)

    handleStatusEvent(ctx)

    expect(failAssistantMessage).toHaveBeenCalledWith('sess-1', serverCopy, 1_700_000_100, null)
    const toast = $notifications.get()[0]
    expect(toast.message).toBe(serverCopy)
    expect(toast.detail).toBeUndefined()
  })

  it('raises no toast for the session on screen when its card explains the code (one event, one surface)', () => {
    const { ctx, failAssistantMessage } = errorContext('[Errno 28] No space left on device')

    handleStatusEvent({ ...ctx, isActiveEvent: true })

    expect(failAssistantMessage).toHaveBeenCalledTimes(1)
    expect($notifications.get()).toEqual([])
  })

  it("still toasts a bare error's server sentence on screen: the card copy is generic", () => {
    const sentence = 'Another Hermes process holds the auth store lock. Wait for it to finish or close it, then retry.'
    const { ctx } = errorContext(sentence)

    handleStatusEvent({ ...ctx, isActiveEvent: true })

    expect($notifications.get()).toEqual([expect.objectContaining({ message: sentence })])
  })
})
