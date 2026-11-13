// [daveey] The renderer's dial wrapper treats Desktop main's answers as advice: a limit main cannot give (not a
// finite positive number, or the bridge throws) falls back to the client default, and a report that throws never
// changes how the dial itself ended.

import { GATEWAY_CONNECT_TIMEOUT_CODE } from '@hermes/shared'
import { describe, expect, it, vi } from 'vitest'

import { dialWithLinkLimit, type GatewayLinkBridge } from './gateway-link-dial'

const WS = 'ws://127.0.0.1:41001/api/ws?ticket=faults'

function connectTimeout(): Error {
  return Object.assign(new Error('Gateway connect timed out: no WebSocket open within 15000 ms'), {
    code: GATEWAY_CONNECT_TIMEOUT_CODE
  })
}

describe('dialWithLinkLimit with a limit main cannot give', () => {
  it('dials with the client default when main answers a non-finite or non-positive limit', async () => {
    for (const limit of [Number.POSITIVE_INFINITY, Number.NaN, 0, -60_000]) {
      const dial = vi.fn(async () => undefined)

      await dialWithLinkLimit(WS, dial, { gatewayConnectLimit: () => limit })

      expect(dial, `limit=${limit}`).toHaveBeenCalledExactlyOnceWith({})
    }
  })

  it('dials with the client default and still reports the open when the limit request throws', async () => {
    const dial = vi.fn(async () => undefined)
    const noteGatewayConnect = vi.fn()

    const bridge: GatewayLinkBridge = {
      gatewayConnectLimit: () => {
        throw new Error('An object could not be cloned.')
      },
      noteGatewayConnect
    }

    await expect(dialWithLinkLimit(WS, dial, bridge, () => 1_000)).resolves.toBeUndefined()

    expect(dial).toHaveBeenCalledExactlyOnceWith({})
    expect(noteGatewayConnect).toHaveBeenCalledExactlyOnceWith(WS, { elapsedMs: 0, kind: 'opened' })
  })
})

describe('dialWithLinkLimit when reporting the outcome throws', () => {
  const throwingReport = (): GatewayLinkBridge => ({
    gatewayConnectLimit: () => 60_000,
    noteGatewayConnect: () => {
      throw new Error('IPC channel closed')
    }
  })

  it('an open dial still resolves', async () => {
    const dial = vi.fn(async () => undefined)

    await expect(dialWithLinkLimit(WS, dial, throwingReport())).resolves.toBeUndefined()
    expect(dial).toHaveBeenCalledExactlyOnceWith({ connectTimeoutMs: 60_000 })
  })

  it('a timed-out dial rejects with its own timeout error, not the report failure', async () => {
    const timeout = connectTimeout()

    await expect(
      dialWithLinkLimit(
        WS,
        async () => {
          throw timeout
        },
        throwingReport()
      )
    ).rejects.toBe(timeout)
  })
})
