// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $pinnedSessionIds, $sidebarShowAutomated } from '@/store/layout'
import { $notifications } from '@/store/notifications'
import {
  $offlineSessions,
  _resetOfflineSessionsForTests,
  type OfflineReplicaRecord,
  offlineSessionsFromReplica
} from '@/store/offline-sessions'
import { $profiles } from '@/store/profile'
import { $sessions } from '@/store/session'
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

  it('refuses to open a cached row, honestly, without calling resume', () => {
    setCached([rec('a')])
    const resume = vi.fn()
    mount(resume)

    fireEvent.click(screen.getByText('Chat a'))

    expect(resume).not.toHaveBeenCalled()
    expect($notifications.get().map(n => n.message).join(' ')).toMatch(/mini is offline/)
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
})
