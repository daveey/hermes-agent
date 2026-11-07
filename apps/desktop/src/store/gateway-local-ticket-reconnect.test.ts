import { JsonRpcGatewayClient } from '@hermes/shared'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { HermesGateway } from '@/api/client'

import {
  activeGateway,
  activeGatewayConnectionId,
  closeSecondaryGateways,
  configureGatewayRegistry,
  liveSecondaryConnectionIds,
  openGatewayForAgent,
  requestGatewayForAgent,
  setPrimaryGateway,
  setPrimaryGatewayConnectionId
} from './gateway'
import { requestForSessionProfile } from './session-request-router'

// Real registry/router/client/native ticket factory; only WebSocket and private
// Electron IPC are simulated. This does not restart a backend process.
const localUrl = 'ws://127.0.0.1:8080/api/ws?native_dial=1'
const remoteUrl = 'wss://remote.invalid/api/ws'
const sockets: Socket[] = []
const clients: JsonRpcGatewayClient[] = []

class Socket extends EventTarget {
  static readonly OPEN = 1
  readyState = 0
  readonly requests: Array<{ id: number; method: string; params: Record<string, unknown> }> = []

  constructor(readonly url: string, readonly protocols?: string[]) {
    super()
    sockets.push(this)
    setTimeout(() => {
      if (this.readyState !== 0) { return }
      this.readyState = 1
      this.dispatchEvent(new Event('open'))
      // A real gateway's first frame after accept; empty = a legacy (non-authority) peer.
      // A native dial is canonical by provenance and ignores it.
      this.dispatchEvent(new MessageEvent('message', {
        data: JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', payload: {} } })
      }))
    }, 1)
  }

  send(data: string): void {
    const request = JSON.parse(data) as (typeof this.requests)[number]
    this.requests.push(request)
    setTimeout(() => this.dispatchEvent(new MessageEvent('message', {
      data: JSON.stringify({ jsonrpc: '2.0', id: request.id, result: { status: 'streaming' } })
    })), 1)
  }

  close(): void {
    if (this.readyState === 3) { return }
    this.readyState = 3
    this.dispatchEvent(new CloseEvent('close', { code: 1000 }))
  }

  drop(): void {
    this.dispatchEvent(new Event('error'))
    this.readyState = 3
    this.dispatchEvent(new CloseEvent('close', { code: 1006 }))
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(Math, 'random').mockReturnValue(0.5)
  vi.stubGlobal('WebSocket', Socket)
  sockets.length = 0
})

afterEach(() => {
  closeSecondaryGateways()

  for (const client of clients.splice(0)) { client.close() }
  setPrimaryGateway(null)
  setPrimaryGatewayConnectionId(null)
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it.each(['factory', 'constructor'] as const)('recovers after a synchronous %s refusal, preserving the original error', async boundary => {
  const refusal = new Error('private ticket unavailable')

  const factory = vi.fn((url: string) => new Socket(url) as unknown as WebSocket)
    .mockImplementationOnce(() => { throw refusal })

  if (boundary === 'constructor') {
    class RefusingSocket extends Socket {
      constructor(url: string) {
        if (factory.mock.calls.length === 0) {
          factory(url)
        }

        super(url)
      }
    }
    vi.stubGlobal('WebSocket', RefusingSocket)
  }

  const client = new JsonRpcGatewayClient(boundary === 'factory' ? { socketFactory: factory } : {})
  clients.push(client)
  await expect(client.connect(remoteUrl)).rejects.toBe(refusal)
  expect.soft(client.connectionState).toBe('closed')
  const connected = client.connect(remoteUrl)
  await vi.advanceTimersByTimeAsync(10)
  await connected
  expect(client.connectionState).toBe('open')
  const response = client.request('session.status', { session_id: 'shared-test' })
  await vi.advanceTimersByTimeAsync(10)
  await expect(response).resolves.toEqual({ status: 'streaming' })
  client.close()
  expect(client.connectionState).toBe('closed')
})

it('surfaces an unminted IPC refusal to the registry caller before opening a socket', async () => {
  const wsUrl = localUrl.replace('native_dial=1', 'native_dial=unminted')

  const failure = new Error('control socket busy')

  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {
    getConnectionFor: vi.fn(async () => ({ mode: 'local', authMode: 'native', profile: 'default', wsUrl })),
    getGatewayWsUrlFor: vi.fn().mockRejectedValue(failure)
  }
  configureGatewayRegistry({ activeConnectionId: () => 'hub', foregroundScopes: () => new Set(), onEvent: vi.fn() })

  await expect(openGatewayForAgent('local', 'default')).rejects.toThrow('control socket busy')
  expect(sockets).toHaveLength(0)
})

it.each([
  { mode: 'local', recovery: 'backoff', descriptor: localUrl },
  { mode: 'local', recovery: 'request', descriptor: localUrl },
  { mode: 'local', recovery: 'backoff', descriptor: localUrl.replace('native_dial=1', 'native_dial=unminted') },
  { mode: 'local', recovery: 'request', descriptor: localUrl.replace('native_dial=1', 'native_dial=unminted') },
  { mode: 'remote', recovery: 'backoff', descriptor: remoteUrl }
] as const)('recovers a retained $mode secondary via $recovery ($descriptor) while the hub primary stays open', async ({ mode, recovery, descriptor: url }) => {
  const owner = { connectionId: mode === 'local' ? 'local' : 'other-remote', mode, profile: 'default' }
  const mint = vi.fn(async () => `${url}${mode === 'local' ? '&ticket=fresh' : ''}`)
  mint.mockResolvedValueOnce(mode === 'local' ? `${url}&ticket=initial` : url)
  // Unminted descriptors surface the IPC failure; legacy native descriptors
  // still fall back and are refused by the real socket factory.
  mint.mockRejectedValueOnce(new Error('gateway restarting'))
  const onActiveRouteChanged = vi.fn()

  const errors = vi.spyOn(console, 'error').mockImplementation(() => {})

  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {
    getConnectionFor: vi.fn(async () => ({ ...owner, authMode: 'token', wsUrl: url })),
    getGatewayWsUrlFor: mint
  }
  configureGatewayRegistry({ activeConnectionId: () => 'hub', foregroundScopes: () => new Set(), onEvent: vi.fn(), onActiveRouteChanged })
  const primary = new HermesGateway()
  clients.push(primary)
  const connected = primary.connect('wss://hub.invalid/api/ws')
  await vi.advanceTimersByTimeAsync(10)
  await connected
  setPrimaryGateway(primary, 'default')
  setPrimaryGatewayConnectionId('hub')
  onActiveRouteChanged.mockClear()
  const opened = openGatewayForAgent(owner.connectionId, owner.profile)
  await vi.advanceTimersByTimeAsync(10)
  await opened
  const original = sockets[1]
  expect(original.readyState).toBe(Socket.OPEN)

  if (mode === 'local') {
    expect(original.protocols).toEqual(['hermes-gateway-v1', 'hermes-gateway-ticket.initial'])
    expect(original.url).toBe(url)
  }

  original.drop()
  // Stop at the first backoff attempt for the explicit-request case; don't
  // accidentally let automatic recovery satisfy that case before its request.
  await vi.advanceTimersByTimeAsync(recovery === 'request' ? 150 : 16_000)
  await vi.dynamicImportSettled()

  if (recovery === 'request') {
    // A user action need not wait for the automatic ladder's next tick.
    expect(sockets).toHaveLength(2)

    const requested = requestGatewayForAgent(owner.connectionId, owner.profile, 'session.status',
      { session_id: 'local-session' }, undefined, undefined, { spawnPriority: 'foreground' })

    await vi.advanceTimersByTimeAsync(10)
    await expect(requested).resolves.toEqual({ status: 'streaming' })
  } else {
    // Failure reporting lazily imports the toast module before scheduling the
    // next backoff. Drain that import, then exercise the next automatic retry.
    await vi.advanceTimersByTimeAsync(16_000)
  }

  if (mode === 'local') {
    if (!url.includes('native_dial=unminted')) {
      expect(errors).toHaveBeenCalledWith(expect.stringContaining('conn:local::default'), expect.objectContaining({ message: 'Native gateway requires a fresh private ticket' }))
    }

    expect(mint.mock.calls.length).toBeGreaterThanOrEqual(3)
  }

  expect(sockets).toHaveLength(3)
  expect(sockets[2].readyState).toBe(Socket.OPEN)

  if (mode === 'local') {
    expect(sockets[2].protocols).toEqual(['hermes-gateway-v1', 'hermes-gateway-ticket.fresh'])
  }

  const ambient = vi.fn()
  const response = requestForSessionProfile(owner, ambient, 'session.status', { session_id: 'local-session' })
  await vi.advanceTimersByTimeAsync(10)
  await expect(response).resolves.toEqual({ status: 'streaming' })
  expect(sockets[2].requests).toContainEqual(expect.objectContaining({ method: 'session.status', params: expect.objectContaining({ session_id: 'local-session' }) }))
  expect(ambient).not.toHaveBeenCalled()
  expect(activeGateway()).toBe(primary)
  expect(activeGatewayConnectionId()).toBe('hub')
  expect(primary.connectionState).toBe('open')
  expect(onActiveRouteChanged).not.toHaveBeenCalled()
  closeSecondaryGateways()
  const mintCount = mint.mock.calls.length
  await vi.advanceTimersByTimeAsync(60_000)
  expect(liveSecondaryConnectionIds()).toEqual(new Set())
  expect(sockets[2].readyState).toBe(3)
  expect(mint).toHaveBeenCalledTimes(mintCount)
  expect(sockets).toHaveLength(3)
})
