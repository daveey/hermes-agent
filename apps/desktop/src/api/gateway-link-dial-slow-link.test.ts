// [daveey] The chat WebSocket's connect limit follows slow-link mode. These tests run the real tracker, the real
// main-side limit/outcome functions, the renderer's dial wrapper and the shared JsonRpcGatewayClient against a fake
// socket on fake timers: no source pins.

import { JsonRpcGatewayClient } from '@hermes/shared'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  GATEWAY_CONNECT_LIMIT_CHANNEL,
  GATEWAY_CONNECT_LIMIT_MS,
  GATEWAY_CONNECT_OUTCOME_CHANNEL,
  gatewayConnectLimitMs,
  noteGatewayConnectOutcome,
  registerGatewayConnectIpc,
  RemoteTransports,
  SLOW_LINK_STRIKE_LIMIT,
  SLOW_LINK_TIMEOUT_CAP_MS,
  SlowLinkTracker
} from '../../electron/slow-link'

import { dialWithLinkLimit, type GatewayLinkBridge } from './gateway-link-dial'

const REMOTE_BASE = 'http://127.0.0.1:41001'
const REMOTE_WS = 'ws://127.0.0.1:41001/api/ws?ticket=t1'
const LOCAL_WS = 'ws://127.0.0.1:52002/api/ws?token=local'
const SCOPE = 'conn:hub::default'

/** A WebSocket stand-in that opens `openAfterMs` after it is constructed (on the fake clock). */
class LateSocket extends EventTarget {
  readyState = 0

  constructor(openAfterMs: number) {
    super()
    setTimeout(() => {
      if (this.readyState === 0) {
        this.readyState = 1
        this.dispatchEvent(new Event('open'))
      }
    }, openAfterMs)
  }

  send(): void {}

  close(): void {
    this.readyState = 3
  }
}

function setup(masterAlive: () => Promise<boolean> | undefined = async () => true) {
  const tracker = new SlowLinkTracker()
  const transports = new RemoteTransports()
  const log = vi.fn()

  transports.remember(REMOTE_BASE, SCOPE, masterAlive)

  const outcomes: Array<Promise<void>> = []

  // What preload + main do over IPC, without Electron: the limit comes from main, the outcome goes back to it.
  const bridge: GatewayLinkBridge = {
    gatewayConnectLimit: wsUrl => gatewayConnectLimitMs(wsUrl, tracker, transports),
    noteGatewayConnect: (wsUrl, outcome) => {
      outcomes.push(noteGatewayConnectOutcome(wsUrl, outcome, log, tracker, transports))
    }
  }

  const enterSlowMode = () => tracker.noteTimeoutWhileAlive(REMOTE_BASE) // what a dispatch probe timeout does

  return { bridge, enterSlowMode, log, outcomes, tracker, transports }
}

function dialAt(bridge: GatewayLinkBridge, wsUrl: string, openAfterMs: number) {
  const client = new JsonRpcGatewayClient({
    heartbeatDeadlineMs: 0,
    heartbeatIntervalMs: 0,
    replay: false,
    socketFactory: () => new LateSocket(openAfterMs) as unknown as WebSocket
  })

  const result = dialWithLinkLimit(wsUrl, dial => client.connect(wsUrl, dial), bridge).then(
    () => 'opened' as const,
    (error: Error) => error
  )

  return { client, result }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('chat WebSocket connect limit', () => {
  it('is 15 s for a remote backend on a normal link and 60 s while its link is slow', () => {
    const { bridge, enterSlowMode } = setup()

    expect(bridge.gatewayConnectLimit!(REMOTE_WS)).toBe(GATEWAY_CONNECT_LIMIT_MS)
    expect(GATEWAY_CONNECT_LIMIT_MS).toBe(15_000)

    enterSlowMode()

    expect(bridge.gatewayConnectLimit!(REMOTE_WS)).toBe(SLOW_LINK_TIMEOUT_CAP_MS)
    expect(SLOW_LINK_TIMEOUT_CAP_MS).toBe(60_000)
  })

  it('stays 15 s for a local backend and a URL main does not know, even while a remote link is slow', () => {
    const { bridge, enterSlowMode } = setup()

    enterSlowMode()

    expect(bridge.gatewayConnectLimit!(LOCAL_WS)).toBe(15_000)
    expect(bridge.gatewayConnectLimit!('not a url')).toBe(15_000)
    expect(bridge.gatewayConnectLimit!(undefined as unknown as string)).toBe(15_000)
  })

  it('a socket that opens at 20 s connects in slow mode', async () => {
    const { bridge, enterSlowMode } = setup()
    enterSlowMode()
    const { client, result } = dialAt(bridge, REMOTE_WS, 20_000)

    await vi.advanceTimersByTimeAsync(20_000)

    await expect(result).resolves.toBe('opened')
    expect(client.connectionState).toBe('open')
  })

  it('the same socket times out at 15 s in normal mode', async () => {
    const { bridge } = setup()
    const { client, result } = dialAt(bridge, REMOTE_WS, 20_000)

    await vi.advanceTimersByTimeAsync(15_000)

    const error = await result
    expect(error).toBeInstanceOf(Error)
    expect((error as Error).message).toContain('no WebSocket open within 15000 ms')
    expect(client.connectionState).toBe('error')
  })

  it('a local backend times out at 15 s even while the remote link is slow', async () => {
    const { bridge, enterSlowMode } = setup()
    enterSlowMode()
    const { result } = dialAt(bridge, LOCAL_WS, 20_000)

    await vi.advanceTimersByTimeAsync(15_000)

    expect(String(await result)).toContain('no WebSocket open within 15000 ms')
  })
})

describe('chat WebSocket outcomes feed the slow-link tracker', () => {
  it('a timeout with the SSH master alive is a strike: it enters slow mode, so the next dial gets 60 s', async () => {
    const { bridge, log, outcomes, tracker } = setup(async () => true)
    const first = dialAt(bridge, REMOTE_WS, 20_000)

    await vi.advanceTimersByTimeAsync(15_000)
    await first.result
    await Promise.all(outcomes)

    expect(tracker.isSlow(REMOTE_BASE)).toBe(true)
    expect(tracker.isScopeSlow(SCOPE)).toBe(true)
    expect(log.mock.calls.flat().join('\n')).toContain('chat WebSocket did not open within its limit')

    const second = dialAt(bridge, REMOTE_WS, 20_000)
    await vi.advanceTimersByTimeAsync(20_000)
    await expect(second.result).resolves.toBe('opened')
  })

  it('a timeout with the SSH master dead is not a strike (a dead transport is never slow)', async () => {
    const { bridge, outcomes, tracker } = setup(async () => false)
    const dial = dialAt(bridge, REMOTE_WS, 20_000)

    await vi.advanceTimersByTimeAsync(15_000)
    await dial.result
    await Promise.all(outcomes)

    expect(tracker.isSlow(REMOTE_BASE)).toBe(false)
  })

  it('a socket that opens is an answer: it clears the strikes a dispatch probe left', async () => {
    const { bridge, enterSlowMode, outcomes } = setup()

    for (let strike = 1; strike < SLOW_LINK_STRIKE_LIMIT; strike++) {
      enterSlowMode()
    }

    const dial = dialAt(bridge, REMOTE_WS, 20_000)
    await vi.advanceTimersByTimeAsync(20_000)
    await expect(dial.result).resolves.toBe('opened')
    await Promise.all(outcomes)

    // Strikes were reset by the open, so one more probe timeout does not give up on the link.
    expect(enterSlowMode()).toEqual({ giveUp: false, strikes: 1 })
  })

  it('an open records an answer sample that can take the link out of slow mode', async () => {
    const { tracker, transports } = setup()
    tracker.noteTimeoutWhileAlive(REMOTE_BASE)

    for (let i = 0; i < 5; i++) {
      await noteGatewayConnectOutcome(
        REMOTE_WS,
        { elapsedMs: 50, kind: 'opened' },
        () => undefined,
        tracker,
        transports
      )
    }

    expect(tracker.isSlow(REMOTE_BASE)).toBe(false)
  })

  it('outcomes for unknown URLs and malformed payloads are ignored', async () => {
    const { tracker, transports } = setup()

    await noteGatewayConnectOutcome(LOCAL_WS, { kind: 'timed-out' }, () => undefined, tracker, transports)
    await noteGatewayConnectOutcome(REMOTE_WS, null, () => undefined, tracker, transports)
    await noteGatewayConnectOutcome(REMOTE_WS, { kind: 'bogus' } as never, () => undefined, tracker, transports)

    expect(tracker.isSlow(REMOTE_BASE)).toBe(false)
  })

  it('a dropped scope no longer maps its ws URL to a remote transport', () => {
    const { enterSlowMode, tracker, transports } = setup()
    enterSlowMode()

    transports.forgetScope(SCOPE)

    expect(gatewayConnectLimitMs(REMOTE_WS, tracker, transports)).toBe(15_000)
  })
})

describe('registerGatewayConnectIpc', () => {
  it('answers the sync limit request and accepts outcome reports', () => {
    const listeners = new Map<string, (event: { returnValue?: unknown }, ...args: unknown[]) => void>()
    registerGatewayConnectIpc({ on: (channel, listener) => listeners.set(channel, listener) }, () => undefined)

    const event: { returnValue?: unknown } = {}
    listeners.get(GATEWAY_CONNECT_LIMIT_CHANNEL)!(event, LOCAL_WS)

    expect(event.returnValue).toBe(15_000)
    expect(() => listeners.get(GATEWAY_CONNECT_OUTCOME_CHANNEL)!({}, LOCAL_WS, { kind: 'timed-out' })).not.toThrow()
  })
})
