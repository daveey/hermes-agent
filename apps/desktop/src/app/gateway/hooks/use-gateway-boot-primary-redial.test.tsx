import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { activeGateway, ensureGatewayForAgent, isActivePrimary, requestGatewayForProfile } from '@/store/gateway'
import { $gatewayState } from '@/store/session'
import { requestForSessionProfile } from '@/store/session-request-router'

import { deferred } from '../../../test/deferred'
import { FakeWebSocket } from '../../../test/fake-gateway-socket'
import type { fakeBootDesktop } from '../../../test/gateway-boot-harness'
import {
  advanceBackoff,
  BootHarness,
  bootHooks,
  flushAsync,
  localPrimaryConn,
  recordSends,
  sentMethods,
  setUpBootHarness,
  tearDownBootHarness
} from '../../../test/gateway-boot-harness'

vi.mock(import('@/store/terminal-backend-warning'), () => ({
  warnIfTerminalBackendUnavailable: vi.fn(async () => false)
}))

// The window primary's socket is owned by useGatewayBoot (fresh descriptor and
// ticket per dial, full-jitter backoff). These cases cover the gaps where that
// owner either stopped redialing (a latch) or was bypassed (owner-pinned
// requests that call the raw primary client). Reproduced against the hook and
// the real HermesGateway; the historical outage (ECONNRESET, then local request
// failures until a reload) is consistent with these mechanisms but did not
// record which one fired.

const OWNER = { connectionId: 'local', profile: 'default' }

const ambient = vi.fn(async () => {
  throw new Error('owner-pinned requests must not use the ambient gateway')
})

let desktop: ReturnType<typeof fakeBootDesktop>
let stopRecording: () => void

beforeEach(() => {
  desktop = setUpBootHarness()
  stopRecording = recordSends()
  ambient.mockClear()
})

afterEach(() => {
  stopRecording()
  tearDownBootHarness()
})

const primarySockets = () => FakeWebSocket.instances.filter(socket => socket.url === localPrimaryConn.wsUrl)
const sendsOf = (method: string) => FakeWebSocket.instances.flatMap(sentMethods).filter(sent => sent === method)

async function bootWithRemoteForeground() {
  render(<BootHarness />)
  await flushAsync()

  let opening!: Promise<boolean>
  act(() => {
    opening = ensureGatewayForAgent('coder-remote', 'coder')
  })
  await flushAsync()
  expect(await opening).toBe(true)
  expect(isActivePrimary()).toBe(false)
}

describe('primary socket owner keeps redialing', () => {
  it('redials a local primary after a close, and again after a failed dial (error)', async () => {
    render(<BootHarness />)
    await flushAsync()

    FakeWebSocket.mode = 'fail'
    act(() => primarySockets()[0].drop())
    await advanceBackoff()
    expect(primarySockets().length).toBeGreaterThan(1)
    expect(activeGateway()?.connectionState).not.toBe('open')

    FakeWebSocket.mode = 'open'
    await advanceBackoff()
    await advanceBackoff()
    expect(activeGateway()?.connectionState).toBe('open')
    expect($gatewayState.get()).toBe('open')
  })

  it('redials a drop that lands while the post-reconnect refresh hangs', async () => {
    let hang = false
    const refreshSessions = () => (hang ? new Promise<void>(() => undefined) : Promise.resolve())
    render(<BootHarness refreshSessions={refreshSessions} />)
    await flushAsync()

    hang = true
    act(() => primarySockets()[0].drop())
    await advanceBackoff()
    expect(primarySockets()).toHaveLength(2)
    expect(activeGateway()?.connectionState).toBe('open')

    // The refresh above never settles. The next drop must still be redialed.
    act(() => primarySockets()[1].drop())
    await advanceBackoff()
    await advanceBackoff()
    expect(primarySockets()).toHaveLength(3)
    expect(activeGateway()?.connectionState).toBe('open')
  })

  it('arms the backoff when the socket dropped while boot was still refreshing', async () => {
    const bootRefresh = deferred<void>()
    render(<BootHarness refreshSessions={() => bootRefresh.promise} />)
    await flushAsync()

    act(() => primarySockets()[0].drop())
    await act(async () => bootRefresh.resolve())
    await advanceBackoff()

    expect(primarySockets()).toHaveLength(2)
    expect(activeGateway()?.connectionState).toBe('open')
  })

  it('arms the backoff when the socket dropped while a connection apply was finishing', async () => {
    let switchRefresh: null | ReturnType<typeof deferred<void>> = null
    const refreshSessions = () => switchRefresh?.promise ?? Promise.resolve()
    render(<BootHarness refreshSessions={refreshSessions} />)
    await flushAsync()

    switchRefresh = deferred<void>()
    act(() => bootHooks.connectionApplied?.())
    await flushAsync()
    expect(primarySockets()).toHaveLength(2)

    act(() => primarySockets()[1].drop())
    await act(async () => switchRefresh!.resolve())
    await advanceBackoff()

    expect(primarySockets()).toHaveLength(3)
    expect(activeGateway()?.connectionState).toBe('open')
  })
})

describe('owner-pinned requests on a closed primary', () => {
  it('sends the next local request on the redialed primary while a remote is foregrounded', async () => {
    await bootWithRemoteForeground()
    const [primary, remote] = FakeWebSocket.instances

    act(() => primary.drop())
    const reply = requestForSessionProfile(OWNER, ambient, 'ping')
    await flushAsync()

    await expect(reply).resolves.toEqual({ pong: true })
    const redialed = primarySockets().at(-1)!
    expect(redialed).not.toBe(primary)
    expect(sentMethods(redialed)).toContain('ping')
    expect(sentMethods(remote)).not.toContain('ping')
    expect(ambient).not.toHaveBeenCalled()
    // Recovery never moves the foreground.
    expect(isActivePrimary()).toBe(false)
  })

  it('coalesces concurrent requests onto one primary dial', async () => {
    await bootWithRemoteForeground()
    const lookups = desktop.getConnection.mock.calls.length

    act(() => primarySockets()[0].drop())

    const replies = [
      requestForSessionProfile(OWNER, ambient, 'ping'),
      requestGatewayForProfile('default', 'ping'),
      requestForSessionProfile(OWNER, ambient, 'ping')
    ]

    await flushAsync()

    await expect(Promise.all(replies)).resolves.toEqual([{ pong: true }, { pong: true }, { pong: true }])
    expect(primarySockets()).toHaveLength(2)
    expect(desktop.getConnection.mock.calls.length).toBe(lookups + 1)
  })

  it('never sends a request aborted while it waited for the redial', async () => {
    await bootWithRemoteForeground()
    const lookup = deferred<Record<string, unknown>>()
    desktop.getConnection.mockReturnValueOnce(lookup.promise)

    act(() => primarySockets()[0].drop())
    const controller = new AbortController()
    const reply = requestForSessionProfile(OWNER, ambient, 'session.steer', {}, 30_000, controller.signal)
    const outcome = reply.catch((error: unknown) => error)
    await flushAsync()

    controller.abort()
    await flushAsync()
    expect(await outcome).toMatchObject({ name: 'AbortError' })

    lookup.resolve(localPrimaryConn)
    await flushAsync()
    expect(activeGateway()).not.toBeNull()
    expect(primarySockets().at(-1)?.readyState).toBe(FakeWebSocket.OPEN)
    expect(sendsOf('session.steer')).toEqual([])
  })

  it('keeps the caller deadline and does not send after it expired', async () => {
    await bootWithRemoteForeground()
    const lookup = deferred<Record<string, unknown>>()
    desktop.getConnection.mockReturnValueOnce(lookup.promise)

    act(() => primarySockets()[0].drop())
    const outcome = requestForSessionProfile(OWNER, ambient, 'session.steer', {}, 1_000).catch(
      (error: unknown) => error
    )
    await flushAsync(1_000)
    expect(String(await outcome)).toMatch(/timed out/i)

    lookup.resolve(localPrimaryConn)
    await flushAsync()
    expect(primarySockets().at(-1)?.readyState).toBe(FakeWebSocket.OPEN)
    expect(sendsOf('session.steer')).toEqual([])
  })

  it('does not replay a request that was already sent when the socket dropped', async () => {
    await bootWithRemoteForeground()
    const reply = requestForSessionProfile(OWNER, ambient, 'session.steer').catch((error: unknown) => error)
    await flushAsync()
    expect(sendsOf('session.steer')).toHaveLength(1)

    act(() => primarySockets()[0].drop())
    expect(String(await reply)).toMatch(/closed|not connected/i)
    await advanceBackoff()

    expect(activeGateway()).not.toBeNull()
    expect(primarySockets().at(-1)?.readyState).toBe(FakeWebSocket.OPEN)
    expect(sendsOf('session.steer')).toHaveLength(1)
  })

  it('does not dial from a request once the boot hook is gone', async () => {
    const view = render(<BootHarness />)
    await flushAsync()
    view.unmount()

    act(() => primarySockets()[0].drop())
    await expect(requestGatewayForProfile('default', 'ping')).rejects.toThrow()
    await flushAsync()
    expect(primarySockets()).toHaveLength(1)
  })

  it('does not race a connection apply with its own dial', async () => {
    render(<BootHarness />)
    await flushAsync()
    const lookup = deferred<Record<string, unknown>>()
    desktop.getConnection.mockReturnValueOnce(lookup.promise)

    act(() => bootHooks.connectionApplied?.())
    await flushAsync()
    const lookups = desktop.getConnection.mock.calls.length

    await expect(requestForSessionProfile(OWNER, ambient, 'ping')).rejects.toThrow()
    expect(desktop.getConnection.mock.calls.length).toBe(lookups)

    lookup.resolve(localPrimaryConn)
    await flushAsync()
    expect(primarySockets()).toHaveLength(2)
    expect(activeGateway()?.connectionState).toBe('open')
  })
})
