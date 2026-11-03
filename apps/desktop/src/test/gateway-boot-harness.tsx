// Shared harness for the sibling use-gateway-boot-*.test.tsx files: the REAL
// useGatewayBoot hook + REAL HermesGateway over FakeWebSocket, a fake Electron
// bridge, and the store resets every case needs. use-gateway-boot.test.tsx is
// over the file-size ratchet, so new boot scenarios live in siblings that share
// this instead of copying it.
import { act, cleanup } from '@testing-library/react'
import { vi } from 'vitest'

import { takeGatewaySurvivor } from '@/app/gateway/hooks/gateway-hmr-survivor'
import { useGatewayBoot } from '@/app/gateway/hooks/use-gateway-boot'
import { $desktopBoot } from '@/store/boot'
import { _resetConnectionsForTests } from '@/store/connections'
import { closeSecondaryGateways } from '@/store/gateway'
import { endGatewaySwitch } from '@/store/gateway-switch'
import { clearNotifications } from '@/store/notifications'
import { $activeGatewayProfile, $profiles } from '@/store/profile'
import { $connection, $gatewayState } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'

import { FakeWebSocket } from './fake-gateway-socket'

/** App-managed local primary: registry identity `local`, like a real Desktop. */
export const localPrimaryConn = {
  authMode: 'token' as const,
  baseUrl: 'http://127.0.0.1:9119',
  connectionId: 'local',
  mode: 'local' as const,
  profile: 'default',
  token: 't',
  wsUrl: 'ws://127.0.0.1:9119/api/ws?token=t'
}

/** A second, remote registry source the user can foreground. */
export const remoteConn = {
  authMode: 'token' as const,
  baseUrl: 'https://coder.example.com',
  connectionId: 'coder-remote',
  mode: 'remote' as const,
  profile: 'coder',
  token: 'c',
  wsUrl: 'wss://coder.example.com/api/ws?token=c'
}

export const bootHooks = {
  connectionApplied: null as null | (() => void)
}

export function fakeBootDesktop(primary: typeof localPrimaryConn = localPrimaryConn) {
  const unsubscribe = () => () => undefined

  return {
    getConnection: vi.fn(async (_profile?: null | string): Promise<Record<string, unknown>> => primary),
    getConnectionFor: vi.fn(async (_route: { connectionId: string; profile: string }) => remoteConn),
    getGatewayWsUrl: vi.fn(async (conn?: { wsUrl?: string }) => conn?.wsUrl ?? primary.wsUrl),
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
    onBackendExit: vi.fn(unsubscribe),
    onConnectionApplied: vi.fn((callback: () => void) => {
      bootHooks.connectionApplied = callback

      return () => {
        bootHooks.connectionApplied = null
      }
    }),
    onPowerResume: vi.fn(unsubscribe),
    revalidateConnection: vi.fn(async () => ({ ok: true, rebuilt: false })),
    onWindowStateChanged: vi.fn(unsubscribe),
    profile: { get: vi.fn(async () => ({ profile: 'default' })) }
  }
}

export interface BootHarnessProps {
  refreshHermesConfig?: () => Promise<void>
  refreshSessions?: () => Promise<void>
}

export function BootHarness({ refreshHermesConfig, refreshSessions }: BootHarnessProps) {
  useGatewayBoot({
    beforeConnectionSwitch: () => undefined,
    handleGatewayEvent: () => undefined,
    handleServerRequest: () => false,
    onConnectionReady: () => undefined,
    onGatewayReady: () => undefined,
    refreshHermesConfig: refreshHermesConfig ?? (async () => undefined),
    refreshSessions: refreshSessions ?? (async () => undefined)
  })

  return null
}

const originalWebSocket = globalThis.WebSocket

function dropSurvivor() {
  // Vitest keeps import.meta.hot truthy, so an unmount parks an open primary.
  takeGatewaySurvivor()?.gateway.close()
}

export function setUpBootHarness(desktop: ReturnType<typeof fakeBootDesktop> = fakeBootDesktop()) {
  dropSurvivor()
  closeSecondaryGateways()
  $activeGatewayProfile.set('default')
  $connection.set(null)
  $profiles.set([])
  $sessionTiles.set([])
  vi.useFakeTimers()
  FakeWebSocket.mode = 'open'
  FakeWebSocket.instances = []
  FakeWebSocket.pingMode = 'pong'
  bootHooks.connectionApplied = null
  clearNotifications()
  ;(globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop
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

  return desktop
}

export function tearDownBootHarness() {
  cleanup()
  dropSurvivor()
  closeSecondaryGateways()
  $activeGatewayProfile.set('default')
  $connection.set(null)
  _resetConnectionsForTests()
  endGatewaySwitch()
  vi.useRealTimers()
  ;(globalThis as { WebSocket: unknown }).WebSocket = originalWebSocket
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
}

/** Let pending awaits AND the queued 0 ms socket open/error fire. */
export async function flushAsync(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

/** Run the next scheduled reconnect attempt (the backoff caps at 15 s). */
export const advanceBackoff = () => flushAsync(15_000)

/** Methods each FakeWebSocket was asked to send, keyed by socket URL order. */
export function sentMethods(socket: FakeWebSocket): string[] {
  return (socket as unknown as { sent?: string[] }).sent ?? []
}

/** Record every frame's method per socket (FakeWebSocket keeps no log itself). */
export function recordSends() {
  const original = FakeWebSocket.prototype.send

  const spy = vi.spyOn(FakeWebSocket.prototype, 'send').mockImplementation(function (this: FakeWebSocket, data) {
    const frame = JSON.parse(data) as { method?: string }

    const self = this as unknown as { sent?: string[] }

    ;(self.sent ??= []).push(String(frame.method))
    original.call(this, data)
  })

  return () => spy.mockRestore()
}
