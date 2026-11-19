import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { paneMirror } from '@/app/chat/pane-mirror'
import { openSession } from '@/app/open-session'
import { useSessionActions } from '@/app/session/hooks/use-session-actions'
import type { ClientSessionState } from '@/app/types'
import * as model from '@/components/pane-shell/tree/model'
import { $parkedTreePanes } from '@/components/pane-shell/tree/parked-panes'
import * as tree from '@/components/pane-shell/tree/store'
import { registry } from '@/contrib/registry'
import { createClientSessionState } from '@/lib/chat-runtime'
import { clearSessionDraft, freshDraftScope } from '@/store/composer'
import { $defaultProfileRoute } from '@/store/default-profile'
import { requestGatewayForAgent } from '@/store/gateway'
import { $newChatConnectionId, $newChatProfile, $newChatRoute } from '@/store/profile'
import * as session from '@/store/session'
import * as states from '@/store/session-states'
import { $tabStripDefault, setTabStripDefault } from '@/store/tabstrip-prefs'

import { $workspaceIsPage } from '../routes'

import { openNewSessionTab } from './new-session-tab'

vi.mock('@/store/profile', async original => ({
  ...(await original<Record<string, unknown>>()),
  ensureGatewayAgent: vi.fn(async () => undefined),
  ensureGatewayProfile: vi.fn(async () => undefined)
}))
vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  activeGatewayConnectionId: vi.fn(() => 'host'),
  requestGatewayForAgent: vi.fn(),
  retainGatewayForAgent: vi.fn(async () => () => undefined)
}))

beforeAll(() => {
  const reg = (id: string, placement: string) =>
    registry.register({ area: 'panes', data: { placement }, id, render: () => null, title: id })

  reg('workspace', 'main')
  reg('terminal', 'right')
  tree.watchContributedPanes()
  paneMirror({
    source: states.$sessionTiles,
    key: tile => tile.storedSessionId,
    prefix: 'session-tile',
    dir: () => 'center',
    minWidth: '20rem',
    title: id => id,
    render: () => null,
    close: states.closeSessionTile
  })()
})

function mountPlus() {
  const ref = <T,>(current: T) => ({ current })

  const updateSessionState = (id: string, updater: (state: ClientSessionState) => ClientSessionState) => {
    const next = updater(states.$sessionStates.get()[id] ?? createClientSessionState())
    states.$sessionStates.set({ ...states.$sessionStates.get(), [id]: next })

    return next
  }

  const { result } = renderHook(() =>
    useSessionActions({
      activeSessionId: 'existing-runtime',
      activeSessionIdRef: ref<string | null>('existing-runtime'),
      busyRef: ref(false),
      creatingSessionRef: ref(false),
      ensureSessionState: () => createClientSessionState(),
      getRouteToken: () => 'route',
      getRoutedStoredSessionId: () => null,
      navigate: vi.fn(),
      requestGateway: vi.fn(async () => ({}) as never),
      resetViewSync: vi.fn(),
      routedSessionId: null,
      runtimeIdByStoredSessionIdRef: ref(new Map()),
      selectedStoredSessionId: null,
      selectedStoredSessionIdRef: ref<string | null>(null),
      sessionStateByRuntimeIdRef: ref(new Map()),
      syncSessionStateToView: vi.fn(),
      updateSessionState
    })
  )

  return result
}

const flush = () => act(async () => new Promise(resolve => setTimeout(resolve, 0)))
const mainGroup = () => model.findGroup(tree.$layoutTree.get()!, 'main')!
const treeMains = () => mainGroup().panes.filter(id => id === 'workspace' || id.startsWith('session-tile:'))
const tileIds = () => states.$sessionTiles.get().map(t => t.storedSessionId)
const parked = (id: string) => $parkedTreePanes.get().has(id)

function reset() {
  states.$sessionTiles.get().forEach(tile => states.discardSessionTile(tile.storedSessionId))
  states.$sessionStates.set({})
  tree.$layoutTree.set(null)
  tree.$activeTreeGroup.set(null)
  $parkedTreePanes.set(new Set())
  session._resetSessionOwnerHintsForTests()
  session.$selectedStoredSessionId.set(null)
  session.setActiveSessionId(null)
  session.setSessions([])
  session.setConnection(null)
  clearSessionDraft(freshDraftScope())
  $workspaceIsPage.set(false)
  $defaultProfileRoute.set(null)
  $newChatRoute.set(null)
  $newChatProfile.set(null)
  $newChatConnectionId.set(null)
  window.localStorage.clear()
}

beforeEach(() => {
  reset()
  tree.declareDefaultTree(model.group(['workspace', 'terminal'], { active: 'workspace', id: 'main' }))
  setTabStripDefault('single')
  vi.mocked(requestGatewayForAgent).mockReset()
  session.$selectedStoredSessionId.set('loaded-chat')
  session.setActiveSessionId('existing-runtime')
})

afterEach(() => {
  cleanup()
  reset()
  vi.clearAllMocks()
})

const open = (id: string) => openSession(id, vi.fn(), 'tab')

describe('Single replaces the active main tab on every new-tab path', () => {
  it('open-in-new-tab (openSession "tab") replaces instead of accumulating', () => {
    open('a')
    expect(treeMains()).toEqual(['session-tile:a'])
    open('b')
    expect(treeMains()).toEqual(['session-tile:b'])
    expect(mainGroup().active).toBe('session-tile:b')
    // Side panel survives; replaced tabs are parked, their owners (tiles) stay.
    expect(mainGroup().panes).toContain('terminal')
    expect(tileIds()).toEqual(['a', 'b'])
    expect(parked('session-tile:a')).toBe(true)
    expect(parked('workspace')).toBe(true)
  })

  it('re-opening a replaced tab fronts it and replaces the current one', () => {
    open('a')
    open('b')
    open('a')
    expect(treeMains()).toEqual(['session-tile:a'])
    expect(parked('session-tile:a')).toBe(false)
    expect(parked('session-tile:b')).toBe(true)
    expect(tileIds()).toEqual(['a', 'b'])
  })

  it('⌘T / "+" replaces the current main tab with the new draft', async () => {
    vi.mocked(requestGatewayForAgent).mockResolvedValue({ session_id: 'rt-1', stored_session_id: 'draft-1', info: {} })
    const actions = mountPlus()
    open('a')
    expect(treeMains()).toEqual(['session-tile:a'])

    openNewSessionTab(actions.current.openNewSessionTile)
    await flush()

    expect(tileIds()).toEqual(['a', 'draft-1'])
    expect(treeMains()).toEqual(['session-tile:draft-1'])
    expect(parked('session-tile:a')).toBe(true)
    expect(mainGroup().panes).toContain('terminal')
  })

  it('closing the visible tab refills the zone with the one it replaced', () => {
    open('a')
    open('b')
    tree.closeTreePane('session-tile:b')
    expect(tileIds()).toEqual(['a'])
    expect(treeMains()).toEqual(['session-tile:a'])
  })

  it('enabling Single collapses accumulated tabs onto the active one; leaving restores them', () => {
    setTabStripDefault('auto')
    open('a')
    open('b')
    expect(treeMains()).toEqual(['workspace', 'session-tile:a', 'session-tile:b'])

    tree.revealTreePane('session-tile:a')
    setTabStripDefault('single')
    expect(treeMains()).toEqual(['session-tile:a'])
    expect(mainGroup().panes).toContain('terminal')
    expect(tileIds()).toEqual(['a', 'b'])

    for (const mode of ['always', 'never', 'auto'] as const) {
      setTabStripDefault(mode)
      expect([...treeMains()].sort()).toEqual(['session-tile:a', 'session-tile:b', 'workspace'])
      expect($parkedTreePanes.get().size).toBe(0)
      setTabStripDefault('single')
      expect(treeMains()).toHaveLength(1)
    }
  })

  it.each(['auto', 'always', 'never'] as const)('%s keeps every main tab (no replacement)', mode => {
    setTabStripDefault(mode)
    expect($tabStripDefault.get()).toBe(mode)
    open('a')
    open('b')
    expect(treeMains()).toEqual(['workspace', 'session-tile:a', 'session-tile:b'])
  })
})
