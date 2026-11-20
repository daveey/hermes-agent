import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TRANSLATIONS } from '@/i18n'
import { errorCardActions } from '@/lib/error-surface'
import { errorCardText } from '@/lib/error-surface-copy'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

// A chat pinned to a walled subscription route is refused before any model runs
// (gateway/run_turn_runner.py `_resolution_failure_reply` → agent.model_routing_walls.held_reply).
const SID = 'session-1'

const HELD =
  '⚠️ openai-codex/gpt-6-astra subscription quota is out until Tue 11-17 21:59 PT. ' +
  'This chat is pinned to it; use /model to switch, or resend after the reset.'

const RAW = 'Subscription quota unavailable for openai-codex/gpt-6-astra; resets at 1794981540.0'
const thread = TRANSLATIONS.en.assistant.thread

let stream: MessageStreamHarness

async function failTurn(payload: Record<string, unknown>) {
  stream = renderMessageStream(SID)
  await act(() => stream.handleEvent({ payload: {}, session_id: SID, type: 'message.start' }))
  await act(() =>
    stream.handleEvent({
      payload: { status: 'error', recoverable: true, error: RAW, failure_reason: 'route_refused', ...payload },
      session_id: SID,
      type: 'message.complete'
    })
  )

  return [...stream.state().messages].reverse().find(m => m.role === 'assistant' && !m.hidden)
}

describe('a pinned route refused before the turn (route_refused)', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it.each([
    ['the backend sentence on the descriptor', { message: HELD }, HELD],
    // An older backend stamps only the code: the turn text is that sentence (the messaging
    // path's appended "Send it again" boundary notice is not part of it).
    [
      'only the turn text (older backend)',
      {},
      `${HELD}\n\nYour request was not processed. Send it again if you still want me to carry it out.`
    ]
  ])('shows the wall, the reset and the way out from %s, not "internal problem"', async (_case, extra, text) => {
    const bubble = await failTurn({
      text,
      error_surface: { layer: 'gateway', code: 'route_refused', retryable: false, ...extra }
    })

    const card = errorCardText(thread, bubble?.errorSurface)
    expect(card.body).toBe(HELD)
    expect(card.body).toContain('Tue 11-17 21:59 PT')
    expect(card.body).not.toContain(thread.errorLayerBodies.gateway)
    expect(card.title).toBe(thread.errorCodes.route_refused.title)
    // A resend repeats the refusal until the reset: the card's button opens the model picker.
    expect(errorCardActions(bubble?.errorSurface)).toEqual({ primary: 'choose_model' })
  })

  it('shows the same card when the agent build itself hit the wall (tui_gateway agent_init path)', async () => {
    // tui_gateway/methods_prompt.py `_agent_init_failure_surface`: no failure_reason, the turn text is
    // turn_error_text(), and the descriptor carries the held sentence as `message`.
    const bubble = await failTurn({
      failure_reason: undefined,
      error: HELD,
      reset_at: 1794981540,
      text:
        "This chat's model is unavailable. Your message was not answered.\n" +
        `Details: ${HELD}\nUse /model to switch, or resend after the reset.`,
      error_surface: { layer: 'gateway', code: 'route_refused', retryable: false, message: HELD }
    })

    const card = errorCardText(thread, bubble?.errorSurface)
    expect(card.title).toBe(thread.errorCodes.route_refused.title)
    expect(card.body).toBe(HELD)
    expect(card.body).not.toContain(thread.errorCodes.agent_init_failed.title)
    expect(errorCardActions(bubble?.errorSurface)).toEqual({ primary: 'choose_model' })
  })

  it('control: a genuine runtime failure keeps the generic gateway card', async () => {
    const bubble = await failTurn({
      text: 'some reply text',
      failure_reason: 'host_error',
      error_surface: { layer: 'gateway', code: 'host_error', retryable: false }
    })

    expect(bubble?.errorSurface?.message).toBeUndefined()
    expect(errorCardText(thread, bubble?.errorSurface)).toEqual({
      title: thread.errorLayers.gateway,
      body: thread.errorLayerBodies.gateway
    })
  })
})
