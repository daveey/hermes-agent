import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type { HermesGateway } from '@/hermes'

import { registerPrimaryGatewayRecovery, waitForPrimaryGateway } from './primary-gateway-recovery'

const disposers: (() => void)[] = []
beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  disposers.splice(0).forEach(dispose => dispose())
  vi.useRealTimers()
})

function client(state = 'closed', synchronousOpen = false) {
  const listeners = new Set<(state: string) => void>()

  const gateway = {
    connectionState: state,
    onState: (callback: (state: string) => void) => {
      listeners.add(callback)

      if (synchronousOpen) {
        gateway.connectionState = 'open'
        callback('open')
      }

      return () => {
        listeners.delete(callback)
      }
    }
  }

  return { gateway: gateway as unknown as HermesGateway, listeners }
}

it('cleans up a synchronously delivered open subscription', async () => {
  const { gateway, listeners } = client('closed', true)
  disposers.push(registerPrimaryGatewayRecovery(gateway, () => true))
  await expect(waitForPrimaryGateway(gateway, 1000, undefined)).resolves.toEqual({ remainingMs: 1000 })
  expect(listeners.size).toBe(0)
  expect(vi.getTimerCount()).toBe(0)
})

it('does not invoke the new owner for an old gateway', async () => {
  const old = client().gateway
  const current = client().gateway
  const recover = vi.fn(() => true)
  disposers.push(registerPrimaryGatewayRecovery(current, recover))
  await expect(waitForPrimaryGateway(old, 1000, undefined)).resolves.toBeNull()
  expect(recover).not.toHaveBeenCalled()
})

it('rejects old waiters and removes subscriptions when ownership is replaced', async () => {
  const { gateway, listeners } = client()
  disposers.push(registerPrimaryGatewayRecovery(gateway, () => true))
  const result = waitForPrimaryGateway(gateway, 1000, undefined).catch(error => error)
  disposers.push(registerPrimaryGatewayRecovery(client().gateway, () => true))
  expect(await result).toBeInstanceOf(Error)
  expect(listeners.size).toBe(0)
  expect(vi.getTimerCount()).toBe(0)
})

it.each(['abort', 'timeout', 'dispose'])('cleans subscriptions after %s', async mode => {
  const { gateway, listeners } = client()
  const dispose = registerPrimaryGatewayRecovery(gateway, () => true)
  disposers.push(dispose)
  const controller = new AbortController()
  const result = waitForPrimaryGateway(gateway, 1000, controller.signal).catch(error => error)

  if (mode === 'abort') {
    controller.abort()
  }

  if (mode === 'timeout') {
    await vi.advanceTimersByTimeAsync(1000)
  }

  if (mode === 'dispose') {
    dispose()
  }
  expect(await result).toMatchObject({ name: mode === 'abort' ? 'AbortError' : 'Error' })
  expect(listeners.size).toBe(0)
  expect(vi.getTimerCount()).toBe(0)
})

it('charges recovery time to the caller deadline', async () => {
  const { gateway, listeners } = client()
  disposers.push(registerPrimaryGatewayRecovery(gateway, () => true))
  const result = waitForPrimaryGateway(gateway, 1000, undefined)
  await vi.advanceTimersByTimeAsync(700)
  ;(gateway as { connectionState: string }).connectionState = 'open'
  listeners.forEach(listener => listener('open'))
  await expect(result).resolves.toEqual({ remainingMs: 300 })
})
