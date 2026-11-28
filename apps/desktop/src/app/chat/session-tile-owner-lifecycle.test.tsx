// Boot the real contribution lifecycle, including connection subscriptions.
import '@/app/contrib/controller'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useSessionTileDelegate } from '@/app/contrib/hooks/use-session-tile-delegate'
import { openSession } from '@/app/open-session'
import { createClientSessionState } from '@/lib/chat-runtime'
import { parseSessionRefValue } from '@/lib/session-refs'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $connection, $gatewayState, _resetSessionOwnerHintsForTests, setSessions } from '@/store/session'
import { $sessionTiles, patchSessionTile, sessionTileDelegate } from '@/store/session-states'

import { resolveSessionRefOwner } from './session-ref-owner'
import { SessionTilePane } from './session-tile'

vi.mock('@/hermes', async original => ({
  ...(await original<Record<string, unknown>>()),
  getLatestSessionMessages: vi.fn(),
  getSession: vi.fn()
}))
vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  requestGatewayForAgent: vi.fn(),
  requestGatewayForProfile: vi.fn()
}))

vi.mock('@/app/chat', () => ({ ChatView: () => <div>Loaded tile</div> }))

const { getLatestSessionMessages, getSession } = await import('@/hermes')
const { requestGatewayForAgent, requestGatewayForProfile } = await import('@/store/gateway')
const ref = 'default@hub-ec2/20261023_040944_37ae77'
const storedId = parseSessionRefValue(ref).sessionId
const owner = { connectionId: 'hermes-hub', profile: 'default', targetProfile: 'default' }

function openRef() {
  const route = resolveSessionRefOwner(ref)
  expect(route).toEqual(owner)
  openSession(storedId, vi.fn(), 'tab', { ownerRoute: route, workspaceMode: 'sessions' })
  expect($sessionTiles.get()).toHaveLength(1)
  expect($sessionTiles.get()[0]?.ownerRoute).toEqual(owner)
}

function mountDelegate() {
  const ambient = vi.fn()
  const states = new Map<string, ReturnType<typeof createClientSessionState>>()
  renderHook(() =>
    useSessionTileDelegate({
      archiveSession: vi.fn(),
      branchLoadedSession: vi.fn(),
      branchStoredSession: vi.fn(),
      executeSlashCommand: vi.fn(),
      removeSession: vi.fn(),
      requestGateway: ambient,
      runtimeIdByStoredSessionIdRef: { current: new Map() },
      sessionStateByRuntimeIdRef: { current: states },
      updateSessionState: (id, update, stored) => {
        const state = update(states.get(id) ?? createClientSessionState(stored ?? null))
        states.set(id, state)

        return state
      }
    } as Parameters<typeof useSessionTileDelegate>[0])
  )

  return { ambient, states }
}

describe('owner-routed session reference tile lifecycle', () => {
  beforeEach(() => {
    _resetSessionOwnerHintsForTests({ storage: true })
    $connectionsRegistry.set({
      connections: [
        { id: 'local', kind: 'local', label: 'This device' },
        { id: 'hermes-hub', kind: 'ssh', label: 'hub (EC2)' }
      ],
      primary: 'local',
      version: 2
    } as never)
    $connection.set({ connectionId: 'local', mode: 'local' } as never)
    vi.mocked(getLatestSessionMessages).mockResolvedValue({
      messages: [{ content: 'owner transcript', role: 'user', timestamp: 1 }],
      session_id: storedId
    } as never)
    vi.mocked(requestGatewayForAgent).mockResolvedValue({
      info: {},
      messages: [],
      messages_omitted: true,
      session_id: 'rt-hub',
      session_key: storedId
    } as never)
  })

  afterEach(() => {
    cleanup()
    $gatewayState.set('idle')
    $sessionTiles.set([])
    setSessions([])
    $connection.set(null)
    $connectionsRegistry.set(null)
    _resetSessionOwnerHintsForTests({ storage: true })
    vi.clearAllMocks()
  })

  it('loads the exact hub ref through its owner and keeps its runtime across ambient refresh and change', async () => {
    openRef()
    const { ambient, states } = mountDelegate()
    const runtimeId = await sessionTileDelegate()!.resumeTile(storedId)
    patchSessionTile(storedId, { runtimeId })
    expect(states.get(runtimeId)?.messages).toHaveLength(1)
    expect(getLatestSessionMessages).toHaveBeenCalledWith(storedId, { connectionId: 'hermes-hub', profile: 'default' })
    expect(requestGatewayForAgent).toHaveBeenCalledWith('hermes-hub', 'default', 'session.resume', {
      session_id: storedId,
      cols: 96,
      omit_messages: true,
      profile: 'default'
    })

    for (const connection of [
      { connectionId: 'local', mode: 'local' },
      { connectionId: 'local', mode: 'local', baseUrl: 'http://127.0.0.1:9220' },
      { connectionId: 'local', mode: 'local', token: 'refreshed-test-token' }
    ]) {
      $connection.set(connection as never)
      expect($sessionTiles.get()[0]?.runtimeId).toBe('rt-hub')
      expect($sessionTiles.get()[0]?.error).toBeUndefined()
      expect($sessionTiles.get()[0]?.ownerRoute).toEqual(owner)
    }

    expect(ambient).not.toHaveBeenCalled()
    expect(requestGatewayForProfile).not.toHaveBeenCalled()
  })

  it('preserves workspace scope switching without poisoning the restored foreign tile', async () => {
    openRef()
    mountDelegate()
    patchSessionTile(storedId, { runtimeId: await sessionTileDelegate()!.resumeTile(storedId) })
    // An actual workspace/backend switch deliberately swaps the visible bucket
    // and restores runtime-less tiles. That is distinct from connection refresh.
    $connection.set({ connectionId: 'other', mode: 'ssh' } as never)
    expect($sessionTiles.get()).toEqual([])
    $connection.set({ connectionId: 'local', mode: 'local' } as never)
    expect($sessionTiles.get()).toHaveLength(1)
    expect($sessionTiles.get()[0]?.ownerRoute).toEqual(owner)
    expect($sessionTiles.get()[0]?.error).toBeUndefined()
    patchSessionTile(storedId, { runtimeId: await sessionTileDelegate()!.resumeTile(storedId) })
    expect($sessionTiles.get()[0]?.runtimeId).toBe('rt-hub')
  })

  it('keeps an owner 404 retryable and the Retry button resumes through the owner', async () => {
    openRef()
    vi.mocked(requestGatewayForAgent).mockRejectedValueOnce(new Error('404: Session not found'))
    vi.mocked(getSession).mockRejectedValue(new Error('404: Session not found'))
    const { ambient } = mountDelegate()
    $gatewayState.set('open')
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SessionTilePane storedSessionId={storedId} />
      </QueryClientProvider>
    )
    await screen.findByText('Session unavailable — you can retry resuming it.')
    expect($sessionTiles.get()[0]?.error).not.toMatch(/wrong backend/i)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect($sessionTiles.get()[0]?.runtimeId).toBe('rt-hub'))
    expect($sessionTiles.get()[0]?.error).toBeUndefined()
    expect(ambient).not.toHaveBeenCalled()
    expect(requestGatewayForAgent).toHaveBeenCalledTimes(2)
  })

  it('reconnect retries an existing wrong-backend error through the explicit owner', async () => {
    openRef()
    patchSessionTile(storedId, {
      error: 'Wrong backend — this session lives on another connection. Reconnect to that backend to open it.'
    })
    const { ambient } = mountDelegate()
    $gatewayState.set('idle')
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SessionTilePane storedSessionId={storedId} />
      </QueryClientProvider>
    )
    expect(requestGatewayForAgent).not.toHaveBeenCalled()
    act(() => $gatewayState.set('open'))
    await waitFor(() => expect($sessionTiles.get()[0]?.runtimeId).toBe('rt-hub'))
    expect($sessionTiles.get()[0]?.error).toBeUndefined()
    expect(ambient).not.toHaveBeenCalled()
  })

  it('never falls back to the ambient gateway when the explicit owner is unreachable', async () => {
    openRef()
    vi.mocked(getLatestSessionMessages).mockRejectedValue(new Error('owner unreachable'))
    vi.mocked(requestGatewayForAgent).mockRejectedValue(new Error('owner unreachable'))
    const { ambient } = mountDelegate()
    await expect(sessionTileDelegate()!.resumeTile(storedId)).rejects.toThrow('owner unreachable')
    expect(ambient).not.toHaveBeenCalled()
    expect(requestGatewayForProfile).not.toHaveBeenCalled()
    expect($sessionTiles.get()[0]?.ownerRoute).toEqual(owner)
    expect($sessionTiles.get()[0]?.runtimeId).toBeUndefined()
  })
})
