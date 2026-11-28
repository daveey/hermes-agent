import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $desktopOnboarding,
  closeManualOnboarding,
  confirmOnboardingModel,
  type OnboardingContext,
  saveOnboardingLocalEndpoint
} from './onboarding'

describe('local endpoint confirmation requires authoritative empty config', () => {
  const writes: unknown[] = []
  const gateway: OnboardingContext['requestGateway'] = async () => ({ ok: true, provider_configured: true }) as never
  const ctx = { requestGateway: gateway }
  const empty = { model: '', provider: '', providers: [] }

  function backend(options: () => Promise<unknown>) {
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        api: async ({ path, body }: { path: string; body?: unknown }) => {
          if (path === '/api/providers/validate') {
            return { ok: true, reachable: true, models: ['local-model'] }
          }

          if (path.startsWith('/api/model/options')) {
            return options()
          }

          if (path === '/api/model/set') {
            writes.push(body)

            return { ok: true }
          }

          throw new Error(path)
        }
      }
    })
  }

  beforeEach(() => {
    writes.length = 0
    $desktopOnboarding.set({
      configured: false,
      flow: { status: 'idle' },
      mode: 'oauth',
      providers: null,
      reason: 'credentials failed',
      requested: true,
      firstRunSkipped: false,
      manual: false,
      localEndpoint: false,
      freeTierReady: false
    })
  })

  afterEach(() => {
    closeManualOnboarding()
    vi.restoreAllMocks()
  })

  it.each(['unavailable', 'missing', 'partial'] as const)('defers %s configuration until Begin', async mode => {
    backend(async () => {
      if (mode === 'unavailable') {
        throw new Error('temporary config read failure')
      }

      return mode === 'missing' ? {} : { ...empty, model: 'existing-model' }
    })

    expect((await saveOnboardingLocalEndpoint('http://localhost:1234/v1', '', ctx)).ok).toBe(true)
    expect(writes).toHaveLength(0)
    expect($desktopOnboarding.get().flow).toMatchObject({
      status: 'confirming_model',
      currentModel: 'local-model',
      deferPersist: true
    })

    await confirmOnboardingModel(ctx)
    expect(writes).toHaveLength(1)
    expect(writes[0]).toMatchObject({ provider: 'custom', model: 'local-model', base_url: 'http://localhost:1234/v1' })
  })

  it('dismisses unknown configuration without writing', async () => {
    backend(async () => {
      throw new Error('temporary config read failure')
    })
    await saveOnboardingLocalEndpoint('http://localhost:1234/v1', '', ctx)
    closeManualOnboarding()
    expect(writes).toHaveLength(0)
    expect($desktopOnboarding.get().flow.status).toBe('idle')
  })

  it('preserves immediate setup on a positively empty first run', async () => {
    backend(async () => empty)
    expect((await saveOnboardingLocalEndpoint('http://localhost:1234/v1', '', ctx)).ok).toBe(true)
    expect(writes).toHaveLength(1)
    expect($desktopOnboarding.get().flow.status).toBe('idle')
  })

  it.each(['empty', 'configured', 'unavailable'] as const)(
    'ignores %s options arriving after dismissal',
    async mode => {
      let release!: () => void
      let entered!: () => void
      const started = new Promise<void>(resolve => {
        entered = resolve
      })
      const pending = new Promise<void>(resolve => {
        release = resolve
      })
      backend(async () => {
        entered()
        await pending

        if (mode === 'unavailable') {
          throw new Error('temporary config read failure')
        }

        return mode === 'empty' ? empty : { ...empty, model: 'existing-model', provider: 'existing-provider' }
      })

      const save = saveOnboardingLocalEndpoint('http://localhost:1234/v1', '', ctx)
      await started
      closeManualOnboarding()
      release()
      expect((await save).ok).toBe(false)
      expect(writes).toHaveLength(0)
      expect($desktopOnboarding.get().flow.status).toBe('idle')
    }
  )
})
