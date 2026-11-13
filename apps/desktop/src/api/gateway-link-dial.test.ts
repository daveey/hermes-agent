import { GATEWAY_CONNECT_TIMEOUT_CODE, JsonRpcGatewayClient } from '@hermes/shared'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { HermesGateway } from './client'

const WS = 'ws://127.0.0.1:41001/api/ws?native_dial=fixture&ticket=fixture'

afterEach(() => {
  vi.restoreAllMocks()
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
})

function bridge(limit: unknown) {
  const desktop = { gatewayConnectLimit: vi.fn(() => limit), noteGatewayConnect: vi.fn() }

  ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

  return desktop
}

describe('HermesGateway.connect uses the connect limit Desktop main gives its ws URL', () => {
  it('passes the slow-link limit to the shared dial and reports the open', async () => {
    const desktop = bridge(60_000)
    const connect = vi.spyOn(JsonRpcGatewayClient.prototype, 'connect').mockResolvedValue()

    const client = new HermesGateway()
    await client.connect(WS)

    expect(desktop.gatewayConnectLimit).toHaveBeenCalledWith(WS)
    expect(connect).toHaveBeenCalledWith(WS, { connectTimeoutMs: 60_000 })
    expect(desktop.noteGatewayConnect).toHaveBeenCalledWith(WS, expect.objectContaining({ kind: 'opened' }))
    client.close()
  })

  it('reports a connect timeout as timed-out and still rejects with it', async () => {
    const desktop = bridge(15_000)

    const timeout = Object.assign(new Error('no WebSocket open within 15000 ms'), {
      code: GATEWAY_CONNECT_TIMEOUT_CODE
    })

    vi.spyOn(JsonRpcGatewayClient.prototype, 'connect').mockRejectedValue(timeout)

    await expect(new HermesGateway().connect(WS)).rejects.toBe(timeout)
    expect(desktop.noteGatewayConnect).toHaveBeenCalledWith(WS, { kind: 'timed-out' })
  })

  it('a non-timeout failure is not reported as a strike', async () => {
    const desktop = bridge(15_000)
    vi.spyOn(JsonRpcGatewayClient.prototype, 'connect').mockRejectedValue(new Error('WebSocket error before open'))

    await expect(new HermesGateway().connect(WS)).rejects.toThrow('error before open')
    expect(desktop.noteGatewayConnect).not.toHaveBeenCalled()
  })

  it.each([undefined, 0, -1, Number.NaN, 'x'])('falls back to the client default when main answers %s', async limit => {
    bridge(limit)
    const connect = vi.spyOn(JsonRpcGatewayClient.prototype, 'connect').mockResolvedValue()

    const client = new HermesGateway()
    await client.connect(WS)

    expect(connect).toHaveBeenCalledWith(WS, {})
    client.close()
  })

  it('dials with the client default when there is no Desktop bridge (browser-hosted, tests)', async () => {
    const connect = vi.spyOn(JsonRpcGatewayClient.prototype, 'connect').mockResolvedValue()

    const client = new HermesGateway()
    await client.connect(WS)

    expect(connect).toHaveBeenCalledWith(WS, {})
    client.close()
  })
})
