import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { HermesGateway } from '@/api/client'
import { useWorkSourceUnavailable } from '@/lib/use-work-source-unavailable'
import { sessionsHost } from '@/sdk/sessions'
import { makeSessionInfo } from '@/test/session-info'

import { $connectionsRegistry } from './connection-registry-state'
import {
  closeSecondaryGateways,
  configureGatewayRegistry,
  ensureGatewayForAgent,
  setPrimaryGateway,
  setPrimaryGatewayConnectionId
} from './gateway'
import { $profiles } from './profile'
import { _resetSessionOwnerHintsForTests, setSessions } from './session'
import { $unavailableWorkSources } from './session-work-source'
import { _resetWorkSourceOwnerForTests } from './work-source-owner'

// The transport and registry are real; only the WebSocket peer is synthetic.
class AuthoritySocket extends EventTarget {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  static instances: AuthoritySocket[] = []
  readyState = AuthoritySocket.CONNECTING
  constructor(readonly url: string) {
    super()
    AuthoritySocket.instances.push(this)
    setTimeout(() => {
      this.readyState = AuthoritySocket.OPEN
      this.dispatchEvent(new Event('open'))
      this.dispatchEvent(
        new MessageEvent('message', {
          data: JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', payload: {} } })
        })
      )
    }, 1)
  }
  send(_data: string): void {}
  close(): void {
    if (this.readyState === AuthoritySocket.CLOSED) {
      return
    }
    this.readyState = AuthoritySocket.CLOSED
    this.dispatchEvent(new CloseEvent('close', { code: 1000 }))
  }
}

let primary: HermesGateway
const row = makeSessionInfo({ id: 'remote-chat', profile: 'worker', connection_id: 'remote' })
const descriptor = {
  connectionId: 'remote',
  mode: 'remote',
  profile: 'worker',
  authMode: 'token',
  token: 'synthetic-only',
  wsUrl: 'wss://remote.example/api/ws?ticket=synthetic',
  baseUrl: 'https://remote.example'
}

function capture() {
  return (['lineage', 'tasks', 'process', 'subagent', 'control'] as const).map(source =>
    sessionsHost.beginWorkSourceRead({ connectionId: 'remote', profile: 'worker', storedSessionId: row.id, source })
  )
}

function succeed(reads: ReturnType<typeof capture>) {
  for (const read of reads) {
    sessionsHost.reportWorkSourceRead({ ...read, available: true })
  }
}

beforeEach(async () => {
  vi.useFakeTimers()
  vi.stubGlobal('WebSocket', AuthoritySocket)
  AuthoritySocket.instances = []
  $connectionsRegistry.set({
    primary: 'local',
    activeConnectionId: 'local',
    connections: [
      { id: 'local', kind: 'local', label: 'Local' },
      { id: 'remote', kind: 'remote', label: 'Remote' }
    ]
  } as never)
  $profiles.set([{ name: 'default' }, { name: 'worker' }] as never)
  configureGatewayRegistry({ activeConnectionId: () => 'local', foregroundScopes: () => new Set(), onEvent: vi.fn() })
  primary = new HermesGateway()
  const opening = primary.connect('wss://local.example/api/ws?ticket=synthetic')
  await vi.advanceTimersByTimeAsync(2)
  await opening
  setPrimaryGateway(primary, 'default')
  setPrimaryGatewayConnectionId('local', 'local')
  window.hermesDesktop = {
    getConnectionFor: vi.fn(async () => descriptor),
    getGatewayWsUrlFor: vi.fn(async () => descriptor.wsUrl)
  } as never
  const secondary = ensureGatewayForAgent('remote', 'worker')
  await vi.advanceTimersByTimeAsync(10)
  expect(await secondary).toBe(true)
  setSessions([row])
})

afterEach(() => {
  _resetWorkSourceOwnerForTests()
  closeSecondaryGateways()
  primary.close()
  setPrimaryGateway(null)
  setPrimaryGatewayConnectionId(null)
  setSessions([])
  $profiles.set([])
  $connectionsRegistry.set(null)
  $unavailableWorkSources.set({})
  _resetSessionOwnerHintsForTests({ storage: true })
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('revokes a mounted secondary owner on close and rejects old replies after real reconnect', async () => {
  const reads = capture()
  expect(reads.every(read => read.authorityEpoch !== undefined)).toBe(true)
  succeed(reads)
  const { result, unmount } = renderHook(() => useWorkSourceUnavailable(row))

  try {
    expect(result.current).toBe(false)
    act(() => AuthoritySocket.instances.at(-1)!.close())
    expect(result.current).toBe(true)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })
    expect(AuthoritySocket.instances.at(-1)!.readyState).toBe(AuthoritySocket.OPEN)
    act(() => succeed(reads))
    expect(result.current).toBe(true)
    act(() => succeed(capture()))
    expect(result.current).toBe(false)
  } finally {
    unmount()
  }
})
