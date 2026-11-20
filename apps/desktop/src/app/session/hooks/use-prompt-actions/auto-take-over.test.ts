import { JsonRpcGatewayError } from '@hermes/shared'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  AUTO_TAKE_OVER_COOLDOWN_MS,
  AUTO_TAKE_OVER_FRESH_MS,
  resetAutoTakeOverForTests,
  shouldAutoTakeOver
} from '@/components/assistant-ui/thread/assistant-message'
import type { ErrorSurface } from '@/lib/error-surface'

import { autoTakeOverFields, ownershipErrorSurface, sessionNotOwnedErrorSurface } from './utils'

// [daveey] A typed send refused because another backend holds the chat only for a turn Hermes started
// itself (auto_take_over) takes the chat over on its own, once per fresh card.

const refused = (data: Record<string, unknown>) =>
  new JsonRpcGatewayError('This chat is active in another Hermes window/terminal.', { code: 4090, data })

const surface = (refusedAt: number): ErrorSurface => ({
  layer: 'gateway',
  code: 'SESSION_NOT_OWNED',
  retryable: false,
  autoTakeOver: true,
  refusedAt
})

afterEach(() => {
  resetAutoTakeOverForTests()
  vi.useRealTimers()
})

describe('autoTakeOverFields', () => {
  it('stamps a flagged refusal with when it happened', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_000_000)
    expect(autoTakeOverFields(refused({ reason: 'SESSION_NOT_OWNED', auto_take_over: true }))).toEqual({
      autoTakeOver: true,
      refusedAt: 1_000_000
    })
    expect(ownershipErrorSurface(refused({ reason: 'SESSION_NOT_OWNED', auto_take_over: true })).errorSurface).toEqual(
      { layer: 'gateway', code: 'SESSION_NOT_OWNED', retryable: false, autoTakeOver: true, refusedAt: 1_000_000 }
    )
  })

  it('stamps the surface submit puts on the card (owner from the Details line, plus the flag)', () => {
    vi.useFakeTimers()
    vi.setSystemTime(2_000_000)

    const err = new JsonRpcGatewayError(
      'This chat is active in another Hermes window/terminal.\nDetails: session s1 opened by desktop on daveey-mini 4m ago.',
      { code: 4090, data: { reason: 'SESSION_NOT_OWNED', auto_take_over: true, holder_turn_kind: 'kanban_notice' } }
    )

    expect(sessionNotOwnedErrorSurface(err, 'fallback')).toEqual({
      layer: 'gateway',
      code: 'SESSION_NOT_OWNED',
      retryable: false,
      owner: { surface: 'desktop', host: 'daveey-mini', age: '4m' },
      autoTakeOver: true,
      refusedAt: 2_000_000
    })
    expect(
      sessionNotOwnedErrorSurface(refused({ reason: 'SESSION_NOT_OWNED' }), 'fallback').autoTakeOver
    ).toBeUndefined()
  })

  it('leaves an unflagged or foreign error alone', () => {
    expect(autoTakeOverFields(refused({ reason: 'SESSION_NOT_OWNED' }))).toEqual({})
    expect(autoTakeOverFields(refused({ reason: 'SESSION_NOT_OWNED', auto_take_over: 'yes' }))).toEqual({})
    expect(autoTakeOverFields(new Error('auto_take_over'))).toEqual({})
  })
})

describe('shouldAutoTakeOver', () => {
  it('acts once on a fresh flagged card', () => {
    expect(shouldAutoTakeOver(surface(1000), 'rt-1', 1000 + 500)).toBe(true)
  })

  it('never acts on an unflagged card, a missing session or an unstamped card', () => {
    expect(shouldAutoTakeOver({ ...surface(1000), autoTakeOver: false }, 'rt-1', 1000)).toBe(false)
    expect(shouldAutoTakeOver(surface(1000), null, 1000)).toBe(false)
    expect(shouldAutoTakeOver({ ...surface(1000), refusedAt: undefined }, 'rt-1', 1000)).toBe(false)
    expect(shouldAutoTakeOver(undefined, 'rt-1', 1000)).toBe(false)
  })

  it('leaves a stale card (a reopened transcript) to the button', () => {
    expect(shouldAutoTakeOver(surface(1000), 'rt-1', 1000 + AUTO_TAKE_OVER_FRESH_MS + 1)).toBe(false)
  })

  it('does not bounce a chat the holder grabbed back within the cooldown', () => {
    expect(shouldAutoTakeOver(surface(1000), 'rt-1', 1000)).toBe(true)
    expect(shouldAutoTakeOver(surface(5000), 'rt-1', 5000)).toBe(false)
    expect(shouldAutoTakeOver(surface(5000), 'rt-2', 5000)).toBe(true)
    const later = 1000 + AUTO_TAKE_OVER_COOLDOWN_MS + 1
    expect(shouldAutoTakeOver(surface(later), 'rt-1', later)).toBe(true)
  })
})
