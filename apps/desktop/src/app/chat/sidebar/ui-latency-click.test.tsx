// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { peekUiLatency, resetUiLatencyForTests } from '@/lib/ui-latency'
import { _resetBootSnapshotForTests } from '@/store/boot-snapshot'
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

// The sidebar row click is the clock of ui.session_switch: the real ChatSidebar → useOfflineFallback door.

const noop = () => {}

const noopAsync = async () => {}

const rec = (id: string): OfflineReplicaRecord => ({
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
  version: 1
})

const mount = (onResumeSession: (id: string) => void) =>
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
  resetUiLatencyForTests()
  _resetBootSnapshotForTests()
  _resetOfflineSessionsForTests()
  $sessions.set([])
  $profiles.set([])
  $connectionsRegistry.set(null)
})

describe('sidebar click starts ui.session_switch', () => {
  it('starts the span for the clicked session, then hands off to resume', () => {
    const resume = vi.fn()
    mount(resume)

    expect(peekUiLatency('ui.session_switch')).toBeNull()
    fireEvent.click(screen.getByText('Live one'))

    expect(resume).toHaveBeenCalledWith('live-1', expect.objectContaining({ id: 'live-1' }))
    expect(peekUiLatency('ui.session_switch')?.ids).toMatchObject({ session_id: 'live-1' })
  })

  it('a click that is refused (offline, nothing cached) leaves no span behind', () => {
    act(() =>
      $offlineSessions.set(offlineSessionsFromReplica([rec('a')], gw => (gw === 'mini' ? 'conn-mini' : undefined)))
    )
    const resume = vi.fn()
    mount(resume)

    fireEvent.click(screen.getByText('Chat a'))

    expect(resume).not.toHaveBeenCalled()
    expect(peekUiLatency('ui.session_switch')).toBeNull()
  })
})
