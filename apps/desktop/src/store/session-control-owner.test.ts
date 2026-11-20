import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $gateway, setPrimaryGateway, setPrimaryGatewayConnection } from '@/store/gateway'
import { $profiles } from '@/store/profile'
import { $projectTree } from '@/store/project-tree'
import { _resetSessionOwnerHintsForTests, setSessions } from '@/store/session'
import { $sessionControlBySession, clearAllSessionControl, refreshSessionControl } from '@/store/session-control'
import { $sessionTiles, clearAllSessionStates, publishSessionState } from '@/store/session-states'
import { makeSessionInfo } from '@/test/session-info'

// 2026-10-30 15:57 PT, chat 20261029_140918_f8947c: after a Desktop restart a
// day-old LOCAL chat was resumed into main. Its only loaded row was the
// sidebar project tree (the capped recents page no longer held it), the resume
// cached that row (open-timing owner stage 22 ms) but persisted no hint (a
// local-primary untagged row resolves to a bare profile, which is not
// hinted), and primary events are untagged, so no runtime owner either. The
// composer's session.control.read then failed closed with
// "Session owner could not be resolved" and the banner stayed for the chat's
// life, although the owner was sitting in the tree row.

const CONTROL = { goal: null, heartbeat: null, loop: null, revision: 'r1', updated_at: 1 }

function treeWith(rows: ReturnType<typeof makeSessionInfo>[]) {
  return [
    { id: 'home', isNoProject: true, label: 'Home', path: null, previewSessions: rows, repos: [], sessionCount: 1 }
  ]
}

describe('session.control.read owner resolution for a resumed chat (t_01a18f128c7f815c)', () => {
  const primaryRequest = vi.fn(async (method: string) =>
    method === 'session.control.read' ? { control: CONTROL } : { ok: true }
  )

  const ambientRequest = vi.fn(async () => ({ control: CONTROL }))
  const logLine = vi.fn()

  beforeEach(() => {
    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {
      connections: { list: async () => null },
      logLine
    }
    $connectionsRegistry.set({
      activeConnectionId: 'local',
      connections: [
        { id: 'local', kind: 'local', label: 'Local' },
        { id: 'mac-mini', kind: 'remote', label: 'mini' }
      ],
      primary: 'local'
    } as never)
    $profiles.set([{ name: 'default' }, { name: 'omar' }] as never)
    setPrimaryGateway(
      {
        onEvent: () => () => undefined,
        onState: () => () => undefined,
        request: primaryRequest,
        connectionState: 'open'
      } as never,
      'default'
    )
    setPrimaryGatewayConnection({ connectionId: 'local' })
    $gateway.set({ request: ambientRequest } as never)
    // The resume binds the main-pane runtime to its stored id (state mirror).
    publishSessionState('rt-main', createClientSessionState('stored-old'))
  })

  afterEach(() => {
    vi.useRealTimers()
    primaryRequest.mockClear()
    ambientRequest.mockClear()
    logLine.mockClear()
    clearAllSessionControl()
    clearAllSessionStates()
    $sessionTiles.set([])
    $projectTree.set([])
    setSessions([])
    $profiles.set([])
    $gateway.set(null as never)
    setPrimaryGateway(null)
    $connectionsRegistry.set(null)
    _resetSessionOwnerHintsForTests({ storage: true })
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('routes the read by the project-tree row when the chat is outside the loaded recents page', async () => {
    $projectTree.set(treeWith([makeSessionInfo({ id: 'stored-old', profile: 'default' })]) as never)

    await refreshSessionControl('rt-main')

    expect($sessionControlBySession.get()['rt-main']).toMatchObject({ capability: 'supported', error: null })
    expect(primaryRequest).toHaveBeenCalledWith('session.control.read', { session_id: 'rt-main' })
    expect(ambientRequest).not.toHaveBeenCalled()
  })

  it('fails closed and logs while the owner is unknown, then re-reads once the owner row lands', async () => {
    // Read before the sidebar has loaded any row for the chat (boot race).
    await refreshSessionControl('rt-main')

    const failed = $sessionControlBySession.get()['rt-main']
    expect(failed?.error).toMatch(/Session owner could not be resolved/)
    // Fail closed: nothing was sent on any socket, least of all the ambient one.
    expect(primaryRequest).not.toHaveBeenCalled()
    expect(ambientRequest).not.toHaveBeenCalled()
    // The miss leaves a trace in desktop.log.
    expect(logLine).toHaveBeenCalledWith(expect.stringMatching(/\[session-control\].*owner unresolved.*rt-main/))

    // The owner row arrives (sidebar list / project tree refresh).
    setSessions([makeSessionInfo({ id: 'stored-old', profile: 'default' })])

    await vi.waitFor(() => {
      expect($sessionControlBySession.get()['rt-main']).toMatchObject({ capability: 'supported', error: null })
    })
    expect(primaryRequest).toHaveBeenCalledWith('session.control.read', { session_id: 'rt-main' })
    expect(ambientRequest).not.toHaveBeenCalled()
  })

  it('keeps failing closed while the owner stays unresolvable', async () => {
    vi.useFakeTimers()
    await refreshSessionControl('rt-main')
    // Unrelated sidebar churn that names no owner for this chat.
    setSessions([makeSessionInfo({ id: 'someone-else', profile: 'omar' })])
    await vi.advanceTimersByTimeAsync(30_000)

    expect($sessionControlBySession.get()['rt-main']?.error).toMatch(/Session owner could not be resolved/)
    expect(primaryRequest).not.toHaveBeenCalled()
    expect(ambientRequest).not.toHaveBeenCalled()
    // One log line per miss, not one per retry check.
    expect(logLine).toHaveBeenCalledTimes(1)
  })
})
