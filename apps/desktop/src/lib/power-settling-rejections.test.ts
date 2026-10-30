import { afterEach, describe, expect, it } from 'vitest'

import { installPowerSettlingRejectionGuard, isPowerSettlingRejection } from './power-settling-rejections'

// The exact message desktop.log recorded for an uncaught skipped read (10-30 10:44 PDT).
const IPC_MESSAGE =
  "Error invoking remote method 'hermes:api': PowerSettlingError: Background read skipped: the machine is asleep or settling after a wake; the next poll retries."

function rejectionEvent(reason: unknown): PromiseRejectionEvent {
  const event = new Event('unhandledrejection', { cancelable: true }) as PromiseRejectionEvent
  Object.defineProperty(event, 'reason', { value: reason })

  return event
}

describe('power-settling rejection guard', () => {
  let uninstall: (() => void) | undefined

  afterEach(() => {
    uninstall?.()
    uninstall = undefined
  })

  it('recognizes the IPC-stripped error by its name in the message, and the main-process code', () => {
    expect(isPowerSettlingRejection(new Error(IPC_MESSAGE))).toBe(true)
    expect(isPowerSettlingRejection({ code: 'POWER_SETTLING' })).toBe(true)
    expect(isPowerSettlingRejection(IPC_MESSAGE)).toBe(true)
    expect(isPowerSettlingRejection(new Error("Error invoking remote method 'hermes:api': 500: boom"))).toBe(false)
    expect(isPowerSettlingRejection(null)).toBe(false)
    expect(isPowerSettlingRejection(undefined)).toBe(false)
  })

  it('swallows an unhandled PowerSettlingError rejection', () => {
    const target = new EventTarget()
    uninstall = installPowerSettlingRejectionGuard(target as unknown as Window)
    const event = rejectionEvent(new Error(IPC_MESSAGE))
    target.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
  })

  it('leaves every other unhandled rejection to the console', () => {
    const target = new EventTarget()
    uninstall = installPowerSettlingRejectionGuard(target as unknown as Window)
    const event = rejectionEvent(new Error('Timed out connecting to Hermes backend'))
    target.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(false)
  })

  it('is installed by the renderer entry', async () => {
    const { default: main } = (await import('../main.tsx?raw')) as { default: string }
    expect(main).toMatch(/^installPowerSettlingRejectionGuard\(\)$/m)
  })
})
