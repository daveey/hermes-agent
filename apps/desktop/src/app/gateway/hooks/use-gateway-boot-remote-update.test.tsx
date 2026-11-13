import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { $desktopBoot } from '@/store/boot'
import { closeSecondaryGateways } from '@/store/gateway'
import { notifyError } from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $gatewayState } from '@/store/session'

import { FakeWebSocket } from '../../../test/fake-gateway-socket'
import { REMOTE_UPDATE_WAIT_MAX_MS } from '../remote-update-wait'

import { takeGatewaySurvivor } from './gateway-hmr-survivor'
import { useGatewayBoot } from './use-gateway-boot'

// [daveey] A remote's own `hermes update` pauses SSH startup. David-MBP 2026-11-13: the hub's update paused it,
// boot landed on "Hermes couldn't start", and the overlay stayed after the update pid exited until Retry was pressed
// by hand. Shares the fake socket with use-gateway-boot.test.tsx; the desktop bridge carries only what boot touches.

vi.mock(import('@/store/notifications'), async importOriginal => ({
  ...(await importOriginal()),
  notifyError: vi.fn()
}))

vi.mock(import('@/store/terminal-backend-warning'), () => ({
  warnIfTerminalBackendUnavailable: vi.fn(async () => false)
}))

const primaryConn = {
  authMode: 'token' as const,
  baseUrl: 'https://vps.example.com',
  connectionId: 'primary-vps',
  profile: 'default',
  token: 't',
  wsUrl: 'wss://vps.example.com/api/ws?token=t'
}

function fakeDesktop() {
  let bootProgressHandler: ((payload: Record<string, unknown>) => void) | null = null
  const unsubscribe = () => () => undefined

  return {
    getConnection: vi.fn(async () => primaryConn),
    getGatewayWsUrl: vi.fn(async (conn?: { wsUrl?: string }) => conn?.wsUrl ?? primaryConn.wsUrl),
    getBootProgress: vi.fn(async () => ({
      error: null,
      fakeMode: false,
      message: '',
      phase: 'init',
      progress: 0,
      retryable: false,
      running: true,
      timestamp: Date.now()
    })),
    onBootProgress: vi.fn(callback => {
      bootProgressHandler = callback

      return () => {
        bootProgressHandler = null
      }
    }),
    emitBootProgress(payload: Record<string, unknown>) {
      bootProgressHandler?.(payload)
    },
    onBackendExit: vi.fn(unsubscribe),
    onConnectionApplied: vi.fn(unsubscribe),
    onPowerResume: vi.fn(unsubscribe),
    revalidateConnection: vi.fn(async () => ({ ok: true, rebuilt: false })),
    onWindowStateChanged: vi.fn(unsubscribe),
    profile: { get: vi.fn(async () => ({ profile: 'default' })) }
  }
}

function Harness() {
  useGatewayBoot({
    beforeConnectionSwitch: () => undefined,
    handleGatewayEvent: () => undefined,
    handleServerRequest: () => false,
    onConnectionReady: () => undefined,
    onGatewayReady: () => undefined,
    refreshHermesConfig: async () => undefined,
    refreshSessions: async () => undefined
  })

  return null
}

function closeSurvivor() {
  try {
    takeGatewaySurvivor()?.gateway.close()
  } catch {
    // ignore
  }
}

const originalWebSocket = globalThis.WebSocket

beforeEach(() => {
  closeSurvivor()
  closeSecondaryGateways()
  $activeGatewayProfile.set('default')
  $connection.set(null)
  vi.useFakeTimers()
  FakeWebSocket.mode = 'open'
  FakeWebSocket.instances = []
  FakeWebSocket.pingMode = 'pong'
  vi.mocked(notifyError).mockReset()
  ;(globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket
  $gatewayState.set('idle')
  $desktopBoot.set({
    error: null,
    fakeMode: false,
    message: '',
    phase: 'init',
    progress: 0,
    running: true,
    timestamp: Date.now(),
    visible: true
  })
})

afterEach(() => {
  cleanup()
  closeSurvivor()
  closeSecondaryGateways()
  $connection.set(null)
  vi.useRealTimers()
  ;(globalThis as { WebSocket: unknown }).WebSocket = originalWebSocket
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
})

async function flushAsync() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

const remoteUpdatePaused = () =>
  new Error(
    "Error invoking remote method 'hermes:connection:for': Error: Remote Hermes update process 1779396 is still running; SSH startup is paused."
  )

it('INITIAL boot during a remote update keeps waiting past the bounded retry budget, then connects without a click once the update exits', async () => {
  let updateRunning = true
  const desktop = fakeDesktop()
  desktop.getConnection = vi.fn(async () => {
    if (updateRunning) {
      throw remoteUpdatePaused()
    }

    return primaryConn
  })
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

  render(<Harness />)
  await flushAsync()

  // Three minutes of update: far past BOOT_RETRY_MAX_ATTEMPTS. Main's snapshot stays retryable:false (the
  // hermes:connection:for path never marks it), yet the overlay never shows and nothing asks for Retry.
  for (let i = 0; i < 36; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })
    expect($desktopBoot.get().error).toBeNull()
  }

  expect(desktop.getConnection.mock.calls.length).toBeGreaterThan(5)
  expect($desktopBoot.get().phase).toBe('renderer.boot.retry')
  expect($desktopBoot.get().message).toMatch(/remote Hermes update/i)
  expect(notifyError).not.toHaveBeenCalled()

  // The remote update pid exits: the next poll dials through and boot completes on its own.
  updateRunning = false
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000)
  })
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_000)
  })

  expect($gatewayState.get()).toBe('open')
  expect($desktopBoot.get().error).toBeNull()
  expect($desktopBoot.get().running).toBe(false)
  expect($desktopBoot.get().visible).toBe(false)
})

it('a remote update that pauses SSH startup past the wait cap still ends in the recovery overlay', async () => {
  const desktop = fakeDesktop()
  desktop.getConnection = vi.fn(async () => {
    throw remoteUpdatePaused()
  })
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

  render(<Harness />)
  await flushAsync()

  await act(async () => {
    await vi.advanceTimersByTimeAsync(REMOTE_UPDATE_WAIT_MAX_MS + 60_000)
  })

  expect($desktopBoot.get().error).toMatch(/SSH startup is paused/)
  expect($desktopBoot.get().running).toBe(false)
})

// PR #2112 review: the cap is hard. A bounded retry after the cap (main's snapshot says retryable, or the stage is
// 'dialing') that meets the same refusal must not open a fresh 30 min wait: the overlay shows within the cap plus
// the bounded retry budget (5 attempts, at most 2 + 4 + 8 + 15 + 15 s of backoff).
it('a retryable boot snapshot past the wait cap spends only the bounded retry budget, then shows the overlay', async () => {
  const desktop = fakeDesktop()
  desktop.getConnection = vi.fn(async () => {
    throw remoteUpdatePaused()
  })
  desktop.getBootProgress = vi.fn(async () => ({
    error: null,
    fakeMode: false,
    message: '',
    phase: 'init',
    progress: 0,
    retryable: true,
    running: true,
    timestamp: Date.now()
  }))
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

  render(<Harness />)
  await flushAsync()

  await act(async () => {
    await vi.advanceTimersByTimeAsync(REMOTE_UPDATE_WAIT_MAX_MS)
  })
  expect($desktopBoot.get().error).toBeNull()

  const callsAtCap = desktop.getConnection.mock.calls.length

  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000 + 44_000 + 1_000)
  })

  // The bounded branch did retry after the cap (its 5 attempts), and then boot failed instead of waiting again.
  expect(desktop.getConnection.mock.calls.length - callsAtCap).toBeGreaterThanOrEqual(5)
  expect(desktop.getBootProgress).toHaveBeenCalled()
  expect($desktopBoot.get().error).toMatch(/SSH startup is paused/)
  expect($desktopBoot.get().running).toBe(false)
})
