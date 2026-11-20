import { expect, it } from 'vitest'

import { TRANSLATIONS } from '@/i18n'

import { parseErrorSurface, sessionNotOwnedSurface } from './error-surface'
import { errorCardText } from './error-surface-copy'

const codes = [
  'provider_policy_blocked',
  'content_policy_blocked',
  'format_error',
  'invalid_response',
  'empty_response',
  'rate_limit',
  'upstream_rate_limit',
  'overloaded',
  'server_error',
  'timeout',
  'ssl_cert_verification'
] as const

it.each(['zh', 'zh-hant', 'ja'] as const)(
  'renders provider errors in %s without losing the failing provider identity',
  locale => {
    for (const layer of ['network', 'provider', 'endpoint', 'streaming'] as const) {
      const surface = parseErrorSurface({ layer, code: 'unknown_failure', retryable: true })
      const copy = errorCardText(TRANSLATIONS[locale].assistant.thread, surface)
      const english = errorCardText(TRANSLATIONS.en.assistant.thread, surface)
      expect(copy.title, layer).not.toBe(english.title)
      expect(copy.body, layer).not.toBe(english.body)
    }

    expect(TRANSLATIONS[locale].assistant.thread.errorGenericProvider).not.toBe(
      TRANSLATIONS.en.assistant.thread.errorGenericProvider
    )

    for (const code of codes) {
      const surface = parseErrorSurface({
        layer: 'provider',
        code,
        provider: 'fixture-provider',
        provider_label: 'Provider Ω',
        retryable: true
      })

      const copy = errorCardText(TRANSLATIONS[locale].assistant.thread, surface)
      const english = errorCardText(TRANSLATIONS.en.assistant.thread, surface)
      expect(copy.title, code).not.toBe(english.title)
      expect(copy.body, code).not.toBe(english.body)
      expect(copy.body, code).toContain('Provider Ω')
      expect(copy.body, code).not.toContain('fixture-provider')
    }
  }
)

it('does not blame a slow reply when the provider could not be reached', () => {
  // The backend stamps `timeout` for refused connections and DNS failures too
  // (agent/error_classifier.py _CONNECTION_MESSAGE_PATTERNS).
  const surface = parseErrorSurface({
    layer: 'provider',
    code: 'timeout',
    provider_label: 'OpenCode Go',
    retryable: true
  })

  const copy = errorCardText(TRANSLATIONS.en.assistant.thread, surface)

  expect(copy.title).toBe("Can't reach OpenCode Go")
  expect(copy.body).toContain("couldn't reach OpenCode Go")
  expect(copy.body).not.toMatch(/in time|slow/i)
})

it('names the machine holding a chat that is open somewhere else', () => {
  const surface = sessionNotOwnedSurface(
    'This chat is open in another Hermes window/terminal. Use it there, or start a new chat here.\n' +
      'Details: session 20261013_205237_240fcc opened by desktop on davids-mac-mini-1 4m ago.'
  )

  expect(surface.owner).toEqual({ age: '4m', host: 'davids-mac-mini-1', surface: 'desktop' })

  const copy = errorCardText(TRANSLATIONS.en.assistant.thread, surface)

  expect(copy.title).toBe('This chat is open somewhere else')
  expect(copy.body).toContain('Hermes desktop on davids-mac-mini-1 (opened 4m ago)')
})

it('keeps the generic owner copy for an older backend that names no machine', () => {
  const surface = sessionNotOwnedSurface('x\nDetails: session s1 opened by cli 2h10m ago.')

  expect(surface.owner).toEqual({ age: '2h10m', surface: 'cli' })
  expect(errorCardText(TRANSLATIONS.en.assistant.thread, surface).body).toBe(
    TRANSLATIONS.en.assistant.thread.errorCodes.SESSION_NOT_OWNED.body
  )
  expect(sessionNotOwnedSurface('no details at all').owner).toBeUndefined()
})

// [daveey] A turn a forced core update stopped does not continue on its own: its card says to send it again.
it('renders a forced-update stop with its own card and a retry', async () => {
  const { errorRecoveryPlan } = await import('./error-surface')
  const surface = parseErrorSurface({ layer: 'gateway', code: 'core_update_forced_stop', retryable: true })
  const copy = errorCardText(TRANSLATIONS.en.assistant.thread, surface)
  expect(copy.title).toBe('Stopped for a forced Hermes update')
  expect(copy.body).toContain('Send your message again to continue.')
  expect(errorRecoveryPlan(surface).retry).toBe(true)
})

// [daveey] A turn the backend stopped because another owner claimed its session lease gets its own
// card (not the generic "internal problem" gateway copy), and Retry stays on.
it('renders a lease-lost turn with its own card and a retry', async () => {
  const { errorRecoveryPlan } = await import('./error-surface')
  const surface = parseErrorSurface({ layer: 'gateway', code: 'session_turn_lease_lost', retryable: true })
  const copy = errorCardText(TRANSLATIONS.en.assistant.thread, surface)
  expect(copy.title).toBe('Hermes stopped this reply')
  expect(copy.body).toContain('took over this chat')
  expect(copy.title).not.toBe(TRANSLATIONS.en.assistant.thread.errorLayers.gateway)
  expect(errorRecoveryPlan(surface).retry).toBe(true)
})
