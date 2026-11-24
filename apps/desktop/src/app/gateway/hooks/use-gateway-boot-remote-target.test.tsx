import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { requestGatewayForAgent } from '@/store/gateway'

import { FakeWebSocket } from '../../../test/fake-gateway-socket'
import {
  advanceBackoff,
  BootHarness,
  flushAsync,
  localPrimaryConn,
  setUpBootHarness,
  tearDownBootHarness
} from '../../../test/gateway-boot-harness'

vi.mock(import('@/store/terminal-backend-warning'), () => ({
  warnIfTerminalBackendUnavailable: vi.fn(async () => false)
}))

// An app-managed SSH primary has a registry identity but predates registryScoped.
const remote = {
  ...localPrimaryConn,
  baseUrl: 'https://hub.example.com',
  connectionId: 'hub',
  inheritsPrimary: true,
  mode: 'remote' as const,
  wsUrl: 'wss://hub.example.com/api/ws?token=t'
}

const target = '20260101_010101_abcdef'
let desktop: ReturnType<typeof setUpBootHarness>

beforeEach(() => {
  desktop = setUpBootHarness()
  // Main's explicit-profile API is LOCAL even when the window primary is SSH.
  desktop.getConnection.mockImplementation(async (profile, opts?: { inheritsPrimary?: boolean }) =>
    opts?.inheritsPrimary || !profile ? remote : localPrimaryConn
  )
  desktop.getConnection.mockResolvedValueOnce(remote)
  desktop.getConnectionFor.mockImplementation(async () => remote as never)
  desktop.getGatewayWsUrl.mockImplementation(async (_profile, opts?: { inheritsPrimary?: boolean }) =>
    opts?.inheritsPrimary ? remote.wsUrl : localPrimaryConn.wsUrl
  )
  desktop.getGatewayWsUrl.mockResolvedValueOnce(remote.wsUrl)
  const send = FakeWebSocket.prototype.send
  vi.spyOn(FakeWebSocket.prototype, 'send').mockImplementation(function (this: FakeWebSocket, data) {
    const frame = JSON.parse(data) as { id: number; method: string }

    if (frame.method !== 'session.resume' && frame.method !== 'prompt.submit') {
      return send.call(this, data)
    }

    const reply =
      this.url === remote.wsUrl ? { result: { session_id: target } } : { error: { code: 4004, message: 'not_found' } }

    const socket = this as unknown as { emit: (type: string, event: unknown) => void }
    socket.emit('message', { data: JSON.stringify({ jsonrpc: '2.0', id: frame.id, ...reply }) })
  })
})

afterEach(() => {
  vi.restoreAllMocks()
  tearDownBootHarness()
})

it('keeps send on the same remote target after a primary disconnect', async () => {
  render(<BootHarness />)
  await flushAsync()
  expect(FakeWebSocket.instances[0].url).toBe(remote.wsUrl)

  act(() => FakeWebSocket.instances[0].drop())
  await advanceBackoff()
  expect(FakeWebSocket.instances.at(-1)?.url).toBe(remote.wsUrl)

  const pending = requestGatewayForAgent('hub', 'default', 'session.resume', { session_id: target }).catch(
    error => error
  )

  await flushAsync()
  const resumed = await pending
  expect(resumed).toEqual({ session_id: target })
  expect(
    await requestGatewayForAgent('hub', 'default', 'prompt.submit', { session_id: target, prompt: 'test' })
  ).toEqual({ session_id: target })
  expect(desktop.getConnection).toHaveBeenCalledWith('default', { inheritsPrimary: true })
  expect(desktop.getGatewayWsUrl).toHaveBeenCalledWith('default', { inheritsPrimary: true })
  expect(FakeWebSocket.instances.every(socket => socket.url === remote.wsUrl)).toBe(true)
})
