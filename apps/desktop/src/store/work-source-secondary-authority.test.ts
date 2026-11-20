import { act, render, renderHook } from '@testing-library/react'
import { createElement } from 'react'
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
import { $workSourceAuthorityRevision, _resetWorkSourceOwnerForTests } from './work-source-owner'

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
    getConnectionFor: vi.fn(async (scope: { profile: string }) => ({ ...descriptor, profile: scope.profile })),
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

it('mounting a previously unread secondary row does not update another row during render', () => {
  const other = makeSessionInfo({ id: 'other-remote-chat', profile: 'worker', connection_id: 'remote' })
  setSessions([row, other])

  function MountedRow() {
    return createElement('span', null, String(useWorkSourceUnavailable(row)))
  }

  let rendering = false
  const renderPublications: number[] = []

  const off = $workSourceAuthorityRevision.listen(value => {
    if (rendering) {
      renderPublications.push(value)
    }
  })

  function NewlyMountedRow() {
    rendering = true
    const unavailable = useWorkSourceUnavailable(other)
    rendering = false

    return createElement('span', null, String(unavailable))
  }

  const first = render(createElement(MountedRow))
  const errors = vi.spyOn(console, 'error')
  const second = render(createElement(NewlyMountedRow))

  try {
    expect(first.container.textContent).toBe('true')
    expect(second.container.textContent).toBe('true')
    expect(renderPublications).toEqual([])
    expect(errors.mock.calls.flat().join(' ')).not.toContain('Cannot update a component')
  } finally {
    off()
    first.unmount()
    second.unmount()
    errors.mockRestore()
  }
})

it('isolates three live connection/profile routes and keeps unaffected complete rows green', async () => {
  const opening = ensureGatewayForAgent('remote', 'default')
  await vi.advanceTimersByTimeAsync(10)
  expect(await opening).toBe(true)
  const local = makeSessionInfo({ id: 'local-chat', profile: 'default', connection_id: 'local' })
  const remoteDefault = makeSessionInfo({ id: 'remote-default-chat', profile: 'default', connection_id: 'remote' })
  setSessions([local, row, remoteDefault])

  const readFor = (session: typeof row) =>
    (['lineage', 'tasks', 'process', 'subagent', 'control'] as const).map(source =>
      sessionsHost.beginWorkSourceRead({
        connectionId: session.connection_id!,
        profile: session.profile!,
        storedSessionId: session.id,
        source
      })
    )

  const localReads = readFor(local)
  const workerReads = readFor(row)
  const otherReads = readFor(remoteDefault)
  expect([...localReads, ...workerReads, ...otherReads].every(read => read.authorityEpoch !== undefined)).toBe(true)
  succeed(localReads)
  succeed(workerReads)

  const mounted = renderHook(() => [
    useWorkSourceUnavailable(local),
    useWorkSourceUnavailable(row),
    useWorkSourceUnavailable(remoteDefault)
  ])

  try {
    expect(mounted.result.current).toEqual([false, false, true])
    act(() => {
      // Identical source names/read counters from another live scope are not proof here.
      for (const read of workerReads) {
        sessionsHost.reportWorkSourceRead({ ...read, storedSessionId: remoteDefault.id, available: true })
      }

      for (const read of localReads) {
        sessionsHost.reportWorkSourceRead({ ...read, storedSessionId: remoteDefault.id, available: true })
      }
    })
    expect(mounted.result.current).toEqual([false, false, true])
    act(() => succeed(otherReads))
    expect(mounted.result.current).toEqual([false, false, false])
    act(() => AuthoritySocket.instances.at(-1)!.close())
    expect(mounted.result.current).toEqual([false, false, true])
  } finally {
    mounted.unmount()
  }
})

it('review: same-id rows on two live routes both retain usable authority', () => {
  const local = makeSessionInfo({ id: row.id, profile: 'default', connection_id: 'local' })
  setSessions([local, row])
  const reads = capture()
  expect(reads.every(read => read.authorityEpoch !== undefined)).toBe(true)
})

it.each([
  ['local, remote-default, worker', 0],
  ['worker, remote-default, local', 1]
])('identical stored ids on three live routes each reach green and recover independently (%s)', async (_label, flip) => {
  const opening = ensureGatewayForAgent('remote', 'default')
  await vi.advanceTimersByTimeAsync(10)
  expect(await opening).toBe(true)
  const remoteDefaultSocket = AuthoritySocket.instances.at(-1)!
  const local = makeSessionInfo({ id: row.id, profile: 'default', connection_id: 'local' })
  const remoteDefault = makeSessionInfo({ id: row.id, profile: 'default', connection_id: 'remote' })
  const routes = [local, remoteDefault, row]
  setSessions(flip ? [...routes].reverse() : routes)

  const readFor = (session: typeof row) =>
    (['lineage', 'tasks', 'process', 'subagent', 'control'] as const).map(source =>
      sessionsHost.beginWorkSourceRead({
        connectionId: session.connection_id!,
        profile: session.profile!,
        storedSessionId: session.id,
        source
      })
    )

  const reads = routes.map(readFor)
  expect(reads.flat().every(read => read.authorityEpoch !== undefined)).toBe(true)
  succeed(reads[0])
  succeed(reads[2])

  const mounted = renderHook(() => routes.map(route => useWorkSourceUnavailable(route)))

  try {
    // Only routes with their own five proofs are green; a sibling's proof never certifies the third.
    expect(mounted.result.current).toEqual([false, true, false])
    act(() => succeed(reads[1]))
    expect(mounted.result.current).toEqual([false, false, false])
    // Closing one live socket vetoes that route only.
    act(() => remoteDefaultSocket.close())
    expect(mounted.result.current).toEqual([false, true, false])
    // Replies captured before the close stay rejected after the real reconnect...
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })
    act(() => succeed(reads[1]))
    expect(mounted.result.current).toEqual([false, true, false])
    // ...and only a fresh complete read set on the healthy route recovers green.
    act(() => succeed(readFor(remoteDefault)))
    expect(mounted.result.current).toEqual([false, false, false])
  } finally {
    mounted.unmount()
  }
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
