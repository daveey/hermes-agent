// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { asleepOpenFor, clearAsleepOpen } from '@/store/asleep-session'
import { _resetBootSnapshotForTests, applyBootSnapshot } from '@/store/boot-snapshot'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $pinnedSessionIds, $sidebarShowAutomated } from '@/store/layout'
import { $notifications } from '@/store/notifications'
import {
  $offlineSessions,
  _resetOfflineSessionsForTests,
  type OfflineReplicaRecord,
  offlineSessionsFromReplica,
  recordSessionGatewayUnavailable
} from '@/store/offline-sessions'
import { $profiles } from '@/store/profile'
import { $messages, $sessions, setMessages } from '@/store/session'
import { clearTranscriptTails, saveTranscriptTail } from '@/store/transcript-tail-cache'
import { makeSessionInfo } from '@/test/session-info'

import { ChatSidebar } from './index'

// [daveey] Cached sessions of an unreachable gateway reach the real sidebar as
// ordinary rows (same filters), marked offline; there is no second section.

const noop = () => {}

const noopAsync = async () => {}

const rec = (id: string, extra: Partial<OfflineReplicaRecord> = {}): OfflineReplicaRecord => ({
  activity: 5_000,
  automated: false,
  gateway: 'mini',
  id,
  key: `default/${id}`,
  local: false,
  message_count: 4,
  profile: 'default',
  source: 'desktop',
  started_at: 4_000,
  stale: false,
  synced_at: Math.floor(Date.now() / 1000) - 300,
  title: `Chat ${id}`,
  version: 1,
  ...extra
})

const setCached = (records: OfflineReplicaRecord[]) =>
  act(() => $offlineSessions.set(offlineSessionsFromReplica(records, gw => (gw === 'mini' ? 'conn-mini' : undefined))))

const mount = (onResumeSession: (id: string) => void = noop) =>
  render(
    <MemoryRouter initialEntries={['/']}>
      <SidebarProvider>
        <ChatSidebar
          currentView="chat"
          onArchiveSession={noop}
          onBranchSession={noop}
          onDeleteSession={noop}
          onLoadMoreSessions={noop}
          onManageCronJob={noop}
          onNavigate={noop}
          onNewSessionInWorkspace={noop}
          onNewSessionSplit={noop}
          onResumeSession={onResumeSession}
          onRetrySessions={noopAsync}
          onTriggerCronJob={noopAsync}
        />
      </SidebarProvider>
    </MemoryRouter>
  )

beforeEach(() => {
  $connectionsRegistry.set({
    version: 2,
    primary: 'local',
    secureTokenStorage: true,
    connections: [
      { id: 'local', label: 'This computer', kind: 'local', tokenSet: false, tokenPreview: null },
      { id: 'conn-mini', label: 'mini', kind: 'remote', tokenSet: true, tokenPreview: null }
    ]
  } as NonNullable<typeof $connectionsRegistry.value>)
  $profiles.set([{ name: 'default', is_default: true }] as typeof $profiles.value)
  $sessions.set([makeSessionInfo({ id: 'live-1', last_active: 9_000, source: 'desktop', title: 'Live one' })])
  $sidebarShowAutomated.set(false)
  $pinnedSessionIds.set([])
  $notifications.set([])
})

afterEach(() => {
  cleanup()
  _resetBootSnapshotForTests()
  _resetOfflineSessionsForTests()
  $sessions.set([])
  $profiles.set([])
  $connectionsRegistry.set(null)
  $sidebarShowAutomated.set(false)
})

describe('ChatSidebar offline fallback rows', () => {
  it('shows an ordinary cached chat as a normal row marked offline, with no standalone section', () => {
    setCached([rec('a')])
    mount()

    expect(screen.getByText('Chat a')).toBeTruthy()
    expect(screen.getByText('Live one')).toBeTruthy()
    expect(screen.getAllByTestId('session-row-offline')).toHaveLength(1)
    expect(screen.getByTestId('session-row-offline').getAttribute('aria-label')).toMatch(/Offline.*mini.*synced 5m ago/)
    expect(screen.queryByText(/Synced from other gateways/i)).toBeNull()
  })

  it('applies the automated/test toggle: bot rows are hidden until it is on', () => {
    setCached([rec('user'), rec('bot', { automated: true })])
    mount()

    expect(screen.getByText('Chat user')).toBeTruthy()
    expect(screen.queryByText('Chat bot')).toBeNull()

    cleanup()
    $sidebarShowAutomated.set(true)
    mount()

    expect(screen.getByText('Chat bot')).toBeTruthy()
  })

  it('keeps cron/subagent/api history and untitled raw ids out of recents', () => {
    setCached([
      rec('cron1', { source: 'cron', title: 'Nightly job' }),
      rec('sub1', { source: 'subagent', title: 'Child task' }),
      rec('raw-id-123', { title: '' }),
      rec('ok')
    ])
    mount()

    expect(screen.queryByText('Nightly job')).toBeNull()
    expect(screen.queryByText('Child task')).toBeNull()
    expect(screen.queryByText('raw-id-123')).toBeNull()
    expect(screen.getByText('Chat ok')).toBeTruthy()
  })

  it('opens a cached row read-only when this Mac has its transcript [daveey]', () => {
    setCached([rec('a')])
    saveTranscriptTail('a', [{ content: 'kept on this mac', id: 1, role: 'user', timestamp: 1 } as never], {
      connectionId: 'conn-mini',
      profile: 'default'
    })
    const resume = vi.fn()
    mount(resume)

    fireEvent.click(screen.getByText('Chat a'))

    expect(resume).toHaveBeenCalledWith('a', expect.objectContaining({ id: 'a', offline_cached: true }))
    expect(asleepOpenFor('a')).toMatchObject({ connectionId: 'conn-mini', gateway: 'mini', profile: 'default' })
    expect($notifications.get()).toHaveLength(0)
    clearTranscriptTails()
    clearAsleepOpen()
  })

  it('refuses to open a cached row with no cached transcript, honestly, without calling resume', () => {
    setCached([rec('a')])
    const resume = vi.fn()
    mount(resume)

    fireEvent.click(screen.getByText('Chat a'))

    expect(resume).not.toHaveBeenCalled()
    expect(
      $notifications
        .get()
        .map(n => n.message)
        .join(' ')
    ).toMatch(/mini is offline/)
  })

  it('a replica row stays refused while its owner list is failing too', () => {
    setCached([rec('a')])
    act(() => recordSessionGatewayUnavailable('conn-mini'))
    const resume = vi.fn()
    mount(resume)

    fireEvent.click(screen.getByText('Chat a'))

    expect(resume).not.toHaveBeenCalled()
    expect(
      $notifications
        .get()
        .map(n => n.message)
        .join(' ')
    ).toMatch(/mini is offline/)
  })

  it('a stale-list row of a busy local backend still opens, naming the connection as not responding', () => {
    act(() => recordSessionGatewayUnavailable('local'))
    const resume = vi.fn()
    mount(resume)

    expect(screen.getByTestId('session-row-offline').getAttribute('aria-label')).toBe(
      'Not responding · This computer, showing its last list'
    )

    fireEvent.click(screen.getByText('Live one'))

    expect(resume).toHaveBeenCalledWith('live-1', expect.objectContaining({ id: 'live-1', offline_stale_list: true }))
    const notices = $notifications
      .get()
      .map(n => n.message)
      .join(' ')
    expect(notices).toMatch(/This computer is not responding/)
    expect(notices).not.toMatch(/its gateway|never synced/)
  })

  it('a remote stale-list row with a cached transcript still resumes live, not read-only', () => {
    $sessions.set([makeSessionInfo({ connection_id: 'conn-mini', id: 'busy', source: 'desktop', title: 'Busy one' })])
    saveTranscriptTail('busy', [{ content: 'kept on this mac', id: 1, role: 'user', timestamp: 1 } as never], {
      connectionId: 'conn-mini',
      profile: 'default'
    })
    act(() => recordSessionGatewayUnavailable('conn-mini'))
    const resume = vi.fn()
    mount(resume)

    fireEvent.click(screen.getByText('Busy one'))

    expect(resume).toHaveBeenCalledWith('busy', expect.objectContaining({ offline_stale_list: true }))
    expect(asleepOpenFor('busy')).toBeNull()
    expect(
      $notifications
        .get()
        .map(n => n.message)
        .join(' ')
    ).toMatch(/^mini is not responding/)
    clearTranscriptTails()
  })

  // [daveey] The boot snapshot carries this Mac's own replica rows. One asleep at boot must not turn a live local row
  // into a refused/read-only cached row when a later local list fails (2026-10-27 incident through the boot path).
  it.each([
    ['no connection_id', undefined],
    ["connection_id 'local'", 'local']
  ])(
    'a local row with an asleep boot-snapshot copy still opens live when the local list fails (%s)',
    (_, connectionId) => {
      $sessions.set([
        makeSessionInfo({
          connection_id: connectionId,
          id: 'live-1',
          last_active: 9_000,
          source: 'desktop',
          title: 'Live one'
        })
      ])
      const asleepAtBoot = rec('live-1', { gateway: 'macbook', local: true, synced_at: 1, title: 'Live one' })
      applyBootSnapshot(
        () =>
          ({
            appStartMs: 1_000,
            hub: { available: true, gateway: 'macbook', sessions: [asleepAtBoot], stale: false, synced_at: 1 },
            readMs: 1,
            tree: null
          }) as never
      )
      act(() => recordSessionGatewayUnavailable('local'))
      const resume = vi.fn()
      mount(resume)

      expect(screen.getAllByText('Live one')).toHaveLength(1)
      expect(screen.getByTestId('session-row-offline').getAttribute('aria-label')).toBe(
        'Not responding · This computer, showing its last list'
      )

      fireEvent.click(screen.getByText('Live one'))

      expect(resume).toHaveBeenCalledWith('live-1', expect.objectContaining({ id: 'live-1', offline_stale_list: true }))
      expect(asleepOpenFor('live-1')).toBeNull()
      const notices = $notifications
        .get()
        .map(n => n.message)
        .join(' ')
      expect(notices).toMatch(/This computer is not responding/)
      expect(notices).not.toMatch(/is offline/)
    }
  )

  it('a remote row whose replica says its owner is asleep stays refused when its list fails too', () => {
    $sessions.set([makeSessionInfo({ connection_id: 'conn-mini', id: 'a', source: 'desktop', title: 'Chat a' })])
    setCached([rec('a')])
    act(() => recordSessionGatewayUnavailable('conn-mini'))
    const resume = vi.fn()
    mount(resume)

    fireEvent.click(screen.getByText('Chat a'))

    expect(resume).not.toHaveBeenCalled()
    expect(
      $notifications
        .get()
        .map(n => n.message)
        .join(' ')
    ).toMatch(/mini is offline/)
  })

  it('opens a live row normally', () => {
    const resume = vi.fn()
    mount(resume)

    fireEvent.click(screen.getByText('Live one'))

    expect(resume).toHaveBeenCalledWith('live-1', expect.objectContaining({ id: 'live-1' }))
  })

  it('reconnect: live rows of that gateway replace the cached ones with no duplicate', () => {
    setCached([rec('a')])
    mount()

    expect(screen.getAllByText('Chat a')).toHaveLength(1)
    expect(screen.getAllByTestId('session-row-offline')).toHaveLength(1)

    act(() =>
      $sessions.set([
        makeSessionInfo({ id: 'live-1', last_active: 9_000, source: 'desktop', title: 'Live one' }),
        makeSessionInfo({ connection_id: 'conn-mini', id: 'a', last_active: 8_000, source: 'desktop', title: 'Chat a' })
      ])
    )

    expect(screen.getAllByText('Chat a')).toHaveLength(1)
    expect(screen.queryAllByTestId('session-row-offline')).toHaveLength(0)
  })

  it("[daveey] paints an awake row's cached transcript in the click, before resume runs", () => {
    // Awake owner (published within the asleep window): an ordinary row that resumes live.
    setCached([rec('w', { synced_at: Math.floor(Date.now() / 1000) - 5 })])
    saveTranscriptTail('w', [{ content: 'cached on click', id: 1, role: 'user', timestamp: 1 } as never], {
      connectionId: 'conn-mini',
      profile: 'default'
    })
    act(() => setMessages([]))
    let onScreenWhenResumed = ''
    mount(() => (onScreenWhenResumed = JSON.stringify($messages.get())))

    fireEvent.click(screen.getByText('Chat w'))

    expect(onScreenWhenResumed).toContain('cached on click')
    clearTranscriptTails()
    act(() => setMessages([]))
  })

  it('[daveey] does not prepaint a row with no cached transcript', () => {
    setCached([rec('n', { synced_at: Math.floor(Date.now() / 1000) - 5 })])
    act(() => setMessages([]))
    let onScreenWhenResumed = 'unset'
    mount(() => (onScreenWhenResumed = JSON.stringify($messages.get())))

    fireEvent.click(screen.getByText('Chat n'))

    expect(onScreenWhenResumed).toBe('[]')
  })
})
