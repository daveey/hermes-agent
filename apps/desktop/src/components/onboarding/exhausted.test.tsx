import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { credentialFailureMetadata } from '@/lib/provider-setup-errors'
import type * as OnboardingModule from '@/store/onboarding'
import {
  $desktopOnboarding,
  closeManualOnboarding,
  consumePendingCredentialWarning,
  requestDesktopOnboarding,
  requestDesktopOnboardingForCredentialWarning,
  startManualOnboarding,
  startManualProviderOAuth
} from '@/store/onboarding'
import type * as OnboardingGateModule from '@/store/onboarding-gate'
import type * as SessionModule from '@/store/session'
import { setModelPickerOpen } from '@/store/session'

import { ExhaustedRecovery } from './exhausted'

vi.mock('@/store/onboarding-gate', async importOriginal => ({
  ...(await importOriginal<typeof OnboardingGateModule>()),
  afterOnboardingStateRead: (callback: () => void) => callback(),
  guidedOnboardingActive: () => false
}))
vi.mock('@/store/onboarding', async importOriginal => ({
  ...(await importOriginal<typeof OnboardingModule>()),
  closeManualOnboarding: vi.fn(),
  startManualOnboarding: vi.fn(),
  startManualProviderOAuth: vi.fn()
}))
vi.mock('@/store/session', async importOriginal => ({
  ...(await importOriginal<typeof SessionModule>()),
  setModelPickerOpen: vi.fn()
}))

const scope = { connectionId: 'remote-owner', profile: 'research' }
const ctx = { scope, requestGateway: vi.fn(async () => undefined as never) }
const failure = { failure_kind: 'exhausted' as const, provider: 'anthropic', reset_at: 1800000000 }

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  consumePendingCredentialWarning(scope)
})

describe('exhausted recovery', () => {
  it('names the provider and real UNIX reset time and opens the existing chat model menu', () => {
    render(<ExhaustedRecovery ctx={ctx} failure={failure} />)
    expect(screen.getByRole('heading').textContent).toBe('anthropic capacity is exhausted')
    expect(screen.getByText(`Expected reset: ${new Date(failure.reset_at * 1000).toLocaleString()}`)).toBeTruthy()
    expect(screen.queryByText("Let's get you setup with Hermes Agent")).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Choose model for this chat' }))
    expect(closeManualOnboarding).toHaveBeenCalledOnce()
    expect(setModelPickerOpen).toHaveBeenCalledWith(true)
    expect(ctx.requestGateway).not.toHaveBeenCalled()
  })

  it('does not invent a reset time and keeps both account and manual provider actions owner-scoped', () => {
    render(<ExhaustedRecovery ctx={ctx} failure={{ ...failure, reset_at: null }} />)
    expect(screen.getByText('The provider has not reported a reset time.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Add account' }))
    expect(startManualProviderOAuth).toHaveBeenCalledWith('anthropic', scope)
    fireEvent.click(screen.getByRole('button', { name: /different provider/i }))
    expect(startManualOnboarding).toHaveBeenCalledWith(null, scope)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(closeManualOnboarding).toHaveBeenCalledOnce()
  })

  it('carries structured warning metadata to requested recovery instead of requiring legacy auth wording', () => {
    requestDesktopOnboardingForCredentialWarning('Quota reached', failure, scope, 'chat')
    const pending = consumePendingCredentialWarning(scope, 'chat')
    expect(pending).toEqual({ reason: 'Quota reached', failure, scope })
    requestDesktopOnboarding('Quota reached', failure, scope)
    expect($desktopOnboarding.get()).toMatchObject({
      exhausted: failure,
      targetScope: scope,
      requested: true,
      manual: false,
      configured: true
    })
  })

  it('does not apply a deferred warning to another connection, profile, or chat', () => {
    for (const [otherScope, session] of [
      [{ ...scope, connectionId: 'other' }, 'chat'],
      [{ ...scope, profile: 'other' }, 'chat'],
      [scope, 'other-chat']
    ] as const) {
      requestDesktopOnboardingForCredentialWarning('Quota reached', failure, scope, 'chat')
      expect(consumePendingCredentialWarning(otherScope, session)).toBeNull()
    }
  })

  it('reads exhaustion from RPC data, not prose, and preserves auth-missing and legacy contracts', () => {
    expect(credentialFailureMetadata({ data: failure })).toEqual(failure)
    expect(credentialFailureMetadata({ data: { failure_kind: 'auth_missing', provider: 'nous' } })).toEqual({
      failure_kind: 'auth_missing',
      provider: 'nous',
      reset_at: null
    })
    expect(credentialFailureMetadata(new Error('Quota reached'))).toBeUndefined()
  })
})
