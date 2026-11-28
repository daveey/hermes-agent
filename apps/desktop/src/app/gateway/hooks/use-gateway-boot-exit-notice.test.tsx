import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { $desktopBoot } from '@/store/boot'
import { closeSecondaryGateways } from '@/store/gateway'
import { $notifications, clearNotifications } from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $gatewayState } from '@/store/session'

import { FakeWebSocket } from '../../../test/fake-gateway-socket'

import { takeGatewaySurvivor } from './gateway-hmr-survivor'
import { BACKEND_EXIT_TOAST_ID, BACKEND_UPDATING_TOAST_ID, useGatewayBoot } from './use-gateway-boot'

// [daveey] The backend-exit notices. hermes-sync's core update SIGTERMs the Desktop-owned backend several times a
// day and main marks that exit `planned: 'update'`; seen 2026-10-29 07:29/08:14/08:38/09:10 as four red "stopped
// working in the background" cards that never cleared. Shares the fake socket with use-gateway-boot.test.tsx.

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

let backendExit: null | ((payload: unknown) => void) = null

function fakeDesktop() {
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
    onBootProgress: vi.fn(unsubscribe),
    onBackendExit: vi.fn(callback => {
      backendExit = callback

      return () => {
        backendExit = null
      }
    }),
    onConnectionApplied: vi.fn(unsubscribe),
    onPowerResume: vi.fn(unsubscribe),
    revalidateConnection: vi.fn(async () => ({ ok: true, rebuilt: false })),
    onWindowStateChanged: vi.fn(unsubscribe),
    touchBackend: vi.fn(async () => undefined),
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
  clearNotifications()
  backendExit = null
  $activeGatewayProfile.set('default')
  $connection.set(null)
  vi.useFakeTimers()
  FakeWebSocket.mode = 'open'
  FakeWebSocket.instances = []
  FakeWebSocket.pingMode = 'pong'
  ;(globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = fakeDesktop()
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
  clearNotifications()
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

const errorIds = () =>
  $notifications
    .get()
    .filter(entry => entry.kind === 'error')
    .map(entry => entry.id)

it('a planned core-update exit after boot shows one quiet updating notice and no crash card', async () => {
  render(<Harness />)
  await flushAsync()
  expect($desktopBoot.get().visible).toBe(false)

  act(() => backendExit?.({ code: null, signal: 'SIGTERM', planned: 'update' }))
  act(() => backendExit?.({ code: null, signal: 'SIGTERM', planned: 'update' }))

  expect(errorIds()).toEqual([])
  expect($notifications.get().map(entry => [entry.id, entry.kind])).toEqual([[BACKEND_UPDATING_TOAST_ID, 'info']])
  expect($notifications.get()[0]?.action).toBeUndefined()
})

it('a planned core-update exit while the boot overlay is up does not fail the boot', async () => {
  const desktop = fakeDesktop()
  desktop.getConnection = vi.fn(() => new Promise<never>(() => undefined))
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

  render(<Harness />)
  await flushAsync()
  expect($desktopBoot.get().visible).toBe(true)

  act(() => backendExit?.({ code: null, signal: 'SIGTERM', planned: 'update' }))

  expect($desktopBoot.get().error).toBeNull()
  expect($notifications.get()).toHaveLength(0)
})

it('an unexpected exit still raises the crash card, and repeated exits keep exactly one', async () => {
  render(<Harness />)
  await flushAsync()

  act(() => backendExit?.({ code: null, signal: 'SIGTERM' }))
  act(() => backendExit?.({ code: 1, signal: null }))
  act(() => backendExit?.({ code: null, signal: 'SIGKILL' }))

  expect(errorIds()).toEqual([BACKEND_EXIT_TOAST_ID])
})

it('the crash card and the updating notice clear once the backend serves again', async () => {
  render(<Harness />)
  await flushAsync()
  expect($gatewayState.get()).toBe('open')

  act(() => backendExit?.({ code: null, signal: 'SIGTERM' }))
  act(() => backendExit?.({ code: null, signal: 'SIGTERM', planned: 'update' }))
  expect(
    $notifications
      .get()
      .map(entry => entry.id)
      .sort()
  ).toEqual([BACKEND_EXIT_TOAST_ID, BACKEND_UPDATING_TOAST_ID].sort())

  // The old child's socket drops; the respawned backend accepts the reconnect.
  act(() => FakeWebSocket.instances.at(-1)!.drop())
  expect($gatewayState.get()).not.toBe('open')
  await act(async () => {
    await vi.advanceTimersByTimeAsync(15_000)
  })

  expect($gatewayState.get()).toBe('open')
  expect($notifications.get()).toHaveLength(0)
})
