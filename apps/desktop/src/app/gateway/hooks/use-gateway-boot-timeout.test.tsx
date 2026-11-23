import { backendReadinessTimeout } from '@hermes/shared'
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { BootFailureOverlay } from '@/components/boot-failure-overlay'
import { en } from '@/i18n/en'
import { BACKEND_BOOT_WAIT_TIMEOUT_MS } from '@/lib/with-timeout'
import { $desktopBoot } from '@/store/boot'
import { closeSecondaryGateways } from '@/store/gateway'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $gatewayState, $selectedStoredSessionId, setSelectedStoredSessionId } from '@/store/session'

import { deferred } from '../../../test/deferred'
import { FakeWebSocket } from '../../../test/fake-gateway-socket'
import { startupConnectionBridge } from '../../../test/startup-connection-bridge'

import { takeGatewaySurvivor } from './gateway-hmr-survivor'
import { useGatewayBoot } from './use-gateway-boot'

// The blocked update-hold screen's renderer leg (R8 D3/M6): a hold the main
// process publishes on boot progress lands in $desktopBoot even after boot.
// Shares the fake socket with use-gateway-boot.test.tsx; the desktop bridge
// here carries only what the boot hook touches.

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

function Harness({ refreshSessions = async () => undefined } = {}) {
  useGatewayBoot({
    beforeConnectionSwitch: () => undefined,
    handleGatewayEvent: () => undefined,
    handleServerRequest: () => false,
    onConnectionReady: () => undefined,
    onGatewayReady: () => undefined,
    refreshHermesConfig: async () => undefined,
    refreshSessions
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
  setSelectedStoredSessionId(null)
  ;(globalThis as { WebSocket: unknown }).WebSocket = originalWebSocket
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
})

async function flushAsync() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

it.each([false, true])('recovers timeout without reload (cross-window reset: %s)', async reset => {
  const desktop = fakeDesktop()
  const pending = deferred<typeof primaryConn>()
  desktop.getConnection.mockImplementationOnce(() => pending.promise)
  Object.assign(desktop, {
    getRecentLogs: async () => ({ lines: [] }),
    getBootstrapState: async () => null,
    getConnectionConfig: async () => ({ mode: 'local' })
  })
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop
  const refreshSessions = vi.fn(async () => undefined)

  const view = render(
    <>
      <Harness refreshSessions={refreshSessions} />
      <BootFailureOverlay />
    </>
  )

  await flushAsync()
  setSelectedStoredSessionId('retained-session')
  await act(async () => {
    await vi.advanceTimersByTimeAsync(BACKEND_BOOT_WAIT_TIMEOUT_MS)
  })
  expect($desktopBoot.get().error).toMatch(/Timed out connecting/)
  expect(screen.getByText(en.boot.failure.title)).toBeTruthy()
  expect($desktopBoot.get().running).toBe(false)

  if (reset) {
    act(() => desktop.emitBootProgress({ phase: 'bootstrap.reset' }))
  }

  await act(async () => {
    await vi.advanceTimersByTimeAsync(reset ? 1 : 2_001)
  })
  await flushAsync()
  expect($desktopBoot.get().error).toBeNull()
  expect($desktopBoot.get().progress).toBe(100)
  expect(screen.queryByText(en.boot.failure.title)).toBeNull()
  expect(refreshSessions).toHaveBeenCalledOnce()
  expect($selectedStoredSessionId.get()).toBe('retained-session')
  const connectionCalls = desktop.getConnection.mock.calls.length
  act(() => desktop.emitBootProgress({ phase: 'bootstrap.reset' }))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000)
  })
  expect(desktop.getConnection).toHaveBeenCalledTimes(connectionCalls)
  expect(refreshSessions).toHaveBeenCalledOnce()
  view.unmount()
})

it.each([false, true])('keeps recovery alive across real IPC wiring (registry route: %s)', async registry => {
  const desktop = fakeDesktop()
  const pending = deferred<typeof primaryConn>()
  const connect = vi.fn(async (_route: unknown) => primaryConn)
  connect.mockImplementationOnce(() => pending.promise)
  connect.mockImplementationOnce(async () => {
    throw backendReadinessTimeout('Gateway starting (deadline). arbitrary diagnostic')
  })
  const bridge = startupConnectionBridge(connect)
  Object.assign(desktop, {
    getStartupConnection: bridge.getStartupConnection,
    profile: { ...desktop.profile, getDefault: async () => registry ? { connectionId: 'local', profile: 'worker' } : null },
    getRecentLogs: async () => ({ lines: [] }), getBootstrapState: async () => null,
    getConnectionConfig: async () => ({ mode: 'local' })
  })
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop
  const refreshSessions = vi.fn(async () => undefined)
  render(<><Harness refreshSessions={refreshSessions} /><BootFailureOverlay /></>)
  setSelectedStoredSessionId('retained-through-ipc')
  await act(async () => { await vi.advanceTimersByTimeAsync(BACKEND_BOOT_WAIT_TIMEOUT_MS) })
  expect($desktopBoot.get().error).toMatch(/Timed out connecting/)
  expect(screen.getByText(en.boot.failure.title)).toBeTruthy()
  await act(async () => { await vi.advanceTimersByTimeAsync(2_001) })
  expect($desktopBoot.get().error).toMatch(/Gateway starting \(deadline\)/)
  expect(screen.getByText(en.boot.failure.title)).toBeTruthy()
  await act(async () => { await vi.advanceTimersByTimeAsync(4_001) })
  expect(connect).toHaveBeenCalledTimes(3)
  expect(refreshSessions).toHaveBeenCalledOnce()
  expect($desktopBoot.get().error).toBeNull()
  expect($desktopBoot.get().progress).toBe(100)
  expect(screen.queryByText(en.boot.failure.title)).toBeNull()
  expect($selectedStoredSessionId.get()).toBe('retained-through-ipc')
  expect(connect.mock.calls[1]?.[0]).toMatchObject(registry ? { connectionId: 'local', profile: 'worker' } : { profile: 'default' })
})

it('stops queued recovery on unmount and ignores a late original readiness reply', async () => {
  const desktop = fakeDesktop()
  const pending = deferred<typeof primaryConn>()
  desktop.getConnection.mockImplementationOnce(() => pending.promise)
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop
  const refreshSessions = vi.fn(async () => undefined)
  const view = render(<Harness refreshSessions={refreshSessions} />)
  await act(async () => {
    await vi.advanceTimersByTimeAsync(BACKEND_BOOT_WAIT_TIMEOUT_MS)
  })
  expect($desktopBoot.get().error).toMatch(/Timed out connecting/)
  view.unmount()
  const calls = desktop.getConnection.mock.calls.length
  pending.resolve(primaryConn)
  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000)
  })
  expect(desktop.getConnection).toHaveBeenCalledTimes(calls)
  expect(refreshSessions).not.toHaveBeenCalled()
  expect($desktopBoot.get().error).toMatch(/Timed out connecting/)
})
