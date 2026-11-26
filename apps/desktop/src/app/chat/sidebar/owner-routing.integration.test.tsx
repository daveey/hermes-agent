import { act, cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $pinnedSessionIds, $sidebarShowAutomated, resetSidebarView, setSidebarAgentsGrouped } from '@/store/layout'
import { $activeGatewayProfile, $profiles } from '@/store/profile'
import { $projectTree } from '@/store/projects'
import { $connection, $sessions, $sessionsLoading } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

import { ChatSidebar } from './index'

const noop = () => {}

const noopAsync = async () => {}

afterEach(() => {
  cleanup()
  resetSidebarView()
  $sessions.set([])
  $projectTree.set([])
  $pinnedSessionIds.set([])
  $connectionsRegistry.set(null)
  $connection.set(null)
  $profiles.set([])
})

it('routes unpinned foreign Kanban owners to Background, never projects or recents', () => {
  resetSidebarView()
  $sidebarShowAutomated.set(true)
  $sessionsLoading.set(false)
  $activeGatewayProfile.set('default')
  $connection.set({ connectionId: 'A', mode: 'local', profile: 'default' } as NonNullable<typeof $connection.value>)
  $connectionsRegistry.set({
    version: 2,
    primary: 'A',
    secureTokenStorage: true,
    connections: [
      { id: 'A', label: 'Gateway A', kind: 'local', tokenSet: false, tokenPreview: null },
      { id: 'B', label: 'Gateway B', kind: 'local', tokenSet: false, tokenPreview: null }
    ]
  } as NonNullable<typeof $connectionsRegistry.value>)
  $profiles.set([{ name: 'default', is_default: true }] as typeof $profiles.value)

  const owners = [
    makeSessionInfo({
      id: 'kanban_owner_shared',
      title: 'A open owner',
      connection_id: 'A',
      kanban_active: 1,
      pinned: true
    }),
    makeSessionInfo({ id: 'kanban_owner_foreign', title: 'B open owner', connection_id: 'B', kanban_active: 1 }),
    makeSessionInfo({ id: 'kanban_owner_closed', title: 'B closed owner', connection_id: 'B' })
  ]

  $sessions.set([...owners, makeSessionInfo({ id: 'ordinary', title: 'Ordinary chat', connection_id: 'B' })])
  $pinnedSessionIds.set(['kanban_owner_shared'])

  const backendOwner = makeSessionInfo({
    id: 'kanban_owner_backend',
    title: 'A closed backend owner',
    connection_id: 'A'
  })

  $projectTree.set([
    {
      id: '__no_project__',
      label: 'Home',
      path: null,
      isNoProject: true,
      previewSessions: [backendOwner],
      sessionIds: [backendOwner.id],
      repos: [],
      sessionCount: 1
    }
  ])
  setSidebarAgentsGrouped(true)

  const { container } = render(
    <MemoryRouter>
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
          onResumeSession={noop}
          onRetrySessions={noopAsync}
          onTriggerCronJob={noopAsync}
        />
      </SidebarProvider>
    </MemoryRouter>
  )

  const assertOwners = () => {
    const background = screen.getByText('Background').closest('[data-sidebar="group"]')!

    for (const title of ['A open owner', 'B open owner']) {
      const row = screen.getByText(title)
      expect(background.contains(row)).toBe(true)
      expect(row.closest('[data-sessions-project]')).toBeNull()
      expect(screen.getAllByText(title)).toHaveLength(1)
    }

    expect(screen.queryByText('B closed owner')).toBeNull()
    expect(screen.queryByText('A closed backend owner')).toBeNull()
    expect(screen.getByText('Ordinary chat')).toBeTruthy()
  }

  expect(container.querySelector('[data-sessions-project]')).not.toBeNull()
  assertOwners()
  act(() => setSidebarAgentsGrouped(false))
  assertOwners()
})
