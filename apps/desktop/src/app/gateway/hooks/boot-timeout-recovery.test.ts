import { backendReadinessTimeout } from '@hermes/shared'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { BootTimeoutRecovery, isBackendReadinessTimeout } from './boot-timeout-recovery'

afterEach(() => vi.useRealTimers())

describe('boot timeout recovery', () => {
  it('backs off from 2 seconds to 30 seconds without overlapping probes', async () => {
    vi.useFakeTimers()
    const recovery = new BootTimeoutRecovery()
    const retry = vi.fn()

    for (const delay of [2_000, 4_000, 8_000, 16_000, 30_000, 30_000]) {
      recovery.schedule(retry)
      await vi.advanceTimersByTimeAsync(delay - 1)
      expect(retry).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(1)
      expect(retry).toHaveBeenCalledOnce()
      retry.mockClear()
      await vi.advanceTimersByTimeAsync(60_000)
      expect(retry).not.toHaveBeenCalled()
    }
  })

  it('expedites a pending retry once and cancels on teardown', async () => {
    vi.useFakeTimers()
    const recovery = new BootTimeoutRecovery()
    const retry = vi.fn()
    recovery.schedule(retry)
    recovery.expedite()
    recovery.expedite()
    expect(retry).toHaveBeenCalledOnce()
    recovery.schedule(retry)
    recovery.stop()
    recovery.expedite()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(retry).toHaveBeenCalledOnce()
    recovery.schedule(retry)
    await vi.advanceTimersByTimeAsync(2_000)
    expect(retry).toHaveBeenCalledTimes(2)
  })

  it('limits automatic recovery to readiness timeouts', () => {
    expect(isBackendReadinessTimeout(backendReadinessTimeout('diagnostic wording is not a contract'), 'connected')).toBe(false)
    expect(isBackendReadinessTimeout(backendReadinessTimeout('diagnostic wording is not a contract'))).toBe(true)
    expect(
      isBackendReadinessTimeout(backendReadinessTimeout('Timed out waiting for Hermes backend port announcement (180000ms)'))
    ).toBe(true)

    for (const error of [
      new Error('Timed out connecting to Hermes backend'),
      new Error('Gateway starting (deadline).'),
      new Error('Sign in again'),
      new Error('Timed out minting the gateway WebSocket URL'),
      null,
      'timeout'
    ]) {
      expect(isBackendReadinessTimeout(error)).toBe(false)
    }
  })

  it('invalidates the old source revision and cancels its queued probe', async () => {
    vi.useFakeTimers()
    const recovery = new BootTimeoutRecovery()
    const retry = vi.fn()
    recovery.schedule(retry)
    recovery.invalidate()
    expect(recovery.revision).toBe(1)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(retry).not.toHaveBeenCalled()
  })
})
