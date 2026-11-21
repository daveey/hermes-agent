import { act, cleanup, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import {
  $dismissedAutoProjectIds,
  $sidebarProjectFilter,
  $sidebarShowAllSessions,
  setSidebarAgentsGrouped
} from '@/store/layout'
import { $projectScope, ALL_PROJECTS } from '@/store/project-scope'
import type * as ProjectsStore from '@/store/projects'
import { $projectTree } from '@/store/projects'
import { $connection, $sessions } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

import type { SidebarProjectTree } from './projects/workspace-groups'

import { ChatSidebar } from './index'

const store = vi.hoisted(() => ({ fetchProjectSessions: vi.fn(), projectProfile: vi.fn(() => 'default') }))
vi.mock('@/store/projects', async original => ({ ...(await original<typeof ProjectsStore>()), ...store }))
const noop = vi.fn()

const local = makeSessionInfo({
  id: 'L',
  connection_id: 'local',
  title: 'NAV LOCAL',
  cwd: '/code/app',
  git_repo_root: '/code/app',
  message_count: 1
})

const remote = makeSessionInfo({
  id: 'R',
  title: 'NAV REMOTE',
  connection_id: 'studio',
  cwd: '/srv/app',
  git_repo_root: '/srv/app',
  message_count: 1
})

// An explicit (user-created) window-gateway project: isAuto is unset.
const base: SidebarProjectTree = {
  id: 'local-app',
  label: 'app',
  path: null,
  repos: [],
  sessionCount: 1,
  previewSessions: [local],
  sessionIds: [local.id]
}

const mount = () =>
  render(
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
          onRetrySessions={noop}
          onTriggerCronJob={async () => {}}
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
      { id: 'local', kind: 'local', label: 'This Mac' },
      { id: 'studio', kind: 'remote', label: 'Studio' }
    ]
  } as never)
  $sidebarShowAllSessions.set(false)
  setSidebarAgentsGrouped(true)
  $sidebarProjectFilter.set([])
  $projectScope.set(ALL_PROJECTS)
  $projectTree.set([base])
  $sessions.set([local, remote])
  store.fetchProjectSessions.mockReset().mockResolvedValue(null)
})
afterEach(() => {
  cleanup()
  $projectTree.set([])
  $sessions.set([])
  $sidebarProjectFilter.set([])
  $dismissedAutoProjectIds.set([])
  $projectScope.set(ALL_PROJECTS)
  setSidebarAgentsGrouped(false)
  $sidebarShowAllSessions.set(true)
  $connection.set(null)
})

it('entered path-less explicit member has no worktree trigger', async () => {
  mount()
  await act(async () => {
    $projectScope.set('local-app')
  })
  await act(async () => {})

  const out = [...document.querySelectorAll('[data-entered-project-member]')].map(el => [
    el.getAttribute('data-entered-project-member'),
    el.querySelectorAll('[aria-label="New worktree"]').length
  ])

  expect(out.find(([id]) => id === 'local-app')![1]).toBe(0)
})

it('entered studio window labels local-stamped and studio members separately', async () => {
  $connection.set({ connectionId: 'studio', mode: 'remote' } as never)
  const own = { ...base, id: 'studio-app', path: '/srv/app', previewSessions: [remote], sessionIds: ['R'] }
  $projectTree.set([own])
  mount()
  await act(async () => {
    $projectScope.set('studio-app')
  })
  await act(async () => {})

  const headings = [...document.querySelectorAll('[data-entered-project-member] span.inline-flex > span.truncate')].map(
    el => el.textContent
  )

  expect(headings.sort()).toEqual(['Studio', 'This Mac'])
})
