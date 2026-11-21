import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import {
  $dismissedAutoProjectIds,
  $sidebarProjectFilter,
  $sidebarShowAllSessions,
  $sidebarShowArchived,
  setSidebarAgentsGrouped
} from '@/store/layout'
import { $projectScope, ALL_PROJECTS } from '@/store/project-scope'
import { $projectTree } from '@/store/projects'
import type * as ProjectsStore from '@/store/projects'
import { $sessions } from '@/store/session'
import { $archivedSessions } from '@/store/sidebar-archive'
import type * as ArchiveStore from '@/store/sidebar-archive'
import { makeSessionInfo } from '@/test/session-info'

import type { SidebarProjectTree } from './projects/workspace-groups'

import { ChatSidebar } from './index'

const store = vi.hoisted(() => ({ fetchProjectSessions: vi.fn(), projectProfile: vi.fn(() => 'default') }))
vi.mock('@/store/projects', async original => ({ ...(await original<typeof ProjectsStore>()), ...store }))
vi.mock('@/store/sidebar-archive', async original => ({
  ...(await original<typeof ArchiveStore>()),
  loadArchivedSessions: vi.fn(async () => {})
}))
vi.mock('./virtual-session-list', () => ({ VirtualSessionList: () => <div data-testid="virtual-recents" /> }))
const noop = vi.fn()

const local = makeSessionInfo({
  id: 'L',
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

const base: SidebarProjectTree = {
  id: 'local-app',
  label: 'app',
  path: '/code/app',
  repos: [{ id: '/code/app', path: '/code/app', label: 'app', groups: [], sessionCount: 1 }],
  sessionCount: 1,
  previewSessions: [local],
  sessionIds: [local.id]
}

const remoteId = JSON.stringify(['gateway-project', 'studio', '/srv/app'])

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
  $connectionsRegistry.set({ version: 2, primary: 'local', secureTokenStorage: true, connections: [] })
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
  $sidebarShowArchived.set(false)
  $archivedSessions.set([])
  $projectTree.set([])
  $sessions.set([])
  $sidebarProjectFilter.set([])
  $dismissedAutoProjectIds.set([])
  $projectScope.set(ALL_PROJECTS)
  setSidebarAgentsGrouped(false)
  $sidebarShowAllSessions.set(true)
})
it.each(['local-app', remoteId])(
  'selecting %s retains every member and entering resolves both pipelines',
  async selected => {
    $sidebarProjectFilter.set([selected])
    mount()
    expect(screen.getAllByText('NAV LOCAL')).toHaveLength(1)
    expect(screen.getAllByText('NAV REMOTE')).toHaveLength(1)
    expect(document.querySelectorAll('[data-presentation-group]')).toHaveLength(1)
    await act(async () => {
      $projectScope.set(selected)
    })
    await waitFor(() => expect(document.querySelectorAll('[data-entered-project-member]')).toHaveLength(2))
    expect($projectScope.get()).toBe(selected)
    expect(screen.getAllByText('NAV LOCAL')).toHaveLength(1)
    expect(screen.getAllByText('NAV REMOTE')).toHaveLength(1)
  }
)
it('a group dismissal hides the explicit primary as well as the auto member and undo restores both', async () => {
  mount()
  expect(screen.getByText('NAV LOCAL')).toBeTruthy()
  await act(async () => {
    $dismissedAutoProjectIds.set(['local-app', remoteId])
  })
  expect(document.querySelector('[data-presentation-group]')).toBeNull()
  // Dismissal is a presentation filter: the durable base tree remains untouched.
  expect($projectTree.get()).toEqual([base])
  await act(async () => {
    $dismissedAutoProjectIds.set([])
  })
  expect(screen.getAllByText('NAV LOCAL')).toHaveLength(1)
  expect(screen.getAllByText('NAV REMOTE')).toHaveLength(1)
})

const cells = [false, true].flatMap(archived =>
  [false, true].flatMap(dismissed =>
    ['primary', 'raw', 'unrelated'].flatMap(selection =>
      [false, true].flatMap(registry =>
        [false, true].flatMap(auto =>
          [false, true].map(reverse => ({ archived, dismissed, selection, registry, auto, reverse }))
        )
      )
    )
  )
)

it('inventories the full navigation cross product with no duplicate cells', () => {
  expect(cells).toHaveLength(96)
  expect(new Set(cells.map(cell => JSON.stringify(cell))).size).toBe(cells.length)
})

it.each(cells)('navigation matrix %j', async cell => {
  const unrelated = makeSessionInfo({ id: 'U', title: 'NAV OTHER', cwd: '/code/other', message_count: 1 })
  const primary = { ...base, id: cell.auto ? '/code/app' : 'p_app', isAuto: cell.auto }
  const other: SidebarProjectTree = {
    id: 'other',
    label: 'other',
    path: '/code/other',
    repos: [],
    sessionCount: 1,
    sessionIds: [unrelated.id],
    previewSessions: [unrelated]
  }
  // A null registry is the supported single-gateway topology: both folders come from that backend.
  // A populated registry exercises the synthetic live foreign member instead.
  const secondRow = { ...remote, connection_id: cell.registry ? 'studio' : undefined }
  const second: SidebarProjectTree = {
    ...base,
    id: remoteId,
    path: '/srv/app',
    isAuto: true,
    sessionIds: [secondRow.id],
    previewSessions: [secondRow]
  }
  const tree = cell.registry ? [primary, other] : [primary, second, other]
  $connectionsRegistry.set(
    cell.registry ? { version: 2, primary: 'local', secureTokenStorage: true, connections: [] } : null
  )
  $projectTree.set(cell.reverse ? [...tree].reverse() : tree)
  const rows = [local, secondRow, unrelated].map(row => ({ ...row, archived: cell.archived }))
  $sessions.set(cell.archived ? [] : rows)
  $archivedSessions.set(cell.archived ? rows : [])
  $sidebarShowArchived.set(cell.archived)
  $dismissedAutoProjectIds.set(cell.dismissed ? [primary.id, remoteId] : [])
  $sidebarProjectFilter.set([
    cell.selection === 'raw' ? remoteId : cell.selection === 'primary' ? primary.id : other.id
  ])
  mount()
  // Dismissal hides project chrome, not the underlying chats: with no selected project left,
  // the unchanged base list falls back to flat recents (Archived is always flat).
  const appVisible = cell.selection !== 'unrelated'
  await waitFor(() => {
    expect(screen.queryAllByText('NAV LOCAL')).toHaveLength(appVisible ? 1 : 0)
    expect(screen.queryAllByText('NAV REMOTE')).toHaveLength(appVisible ? 1 : 0)
    expect(screen.queryAllByText('NAV OTHER')).toHaveLength(cell.selection === 'unrelated' ? 1 : 0)
  })
  expect(document.querySelectorAll('[data-presentation-group]')).toHaveLength(
    appVisible && !cell.archived && !cell.dismissed ? 1 : 0
  )
  expect($projectTree.get()).toEqual(cell.reverse ? [...tree].reverse() : tree)
})

it('exits a stale member scope only after the loaded catalog no longer contains that member', async () => {
  $projectScope.set(remoteId)
  mount()
  await waitFor(() => expect(document.querySelectorAll('[data-entered-project-member]')).toHaveLength(2))
  expect($projectScope.get()).toBe(remoteId)
  await act(async () => {
    $sessions.set([local])
  })
  await waitFor(() => expect($projectScope.get()).toBe(ALL_PROJECTS))
  expect(screen.getAllByText('NAV LOCAL')).toHaveLength(1)
})

it('index recents virtualization exits with grouping, never swallowing entered member content', async () => {
  $sidebarShowAllSessions.set(true)
  $sessions.set([
    local,
    remote,
    ...Array.from({ length: 30 }, (_, i) =>
      makeSessionInfo({ id: `long-${i}`, title: `LONG ${i}`, cwd: '/code/app', message_count: 1 })
    )
  ])
  $projectScope.set(remoteId)
  mount()
  await waitFor(() => expect(document.querySelectorAll('[data-entered-project-member]')).toHaveLength(2))
  expect(screen.queryByTestId('virtual-recents')).toBeNull()
  expect(screen.getAllByText('NAV REMOTE')).toHaveLength(1)
  await act(async () => {
    setSidebarAgentsGrouped(false)
  })
  await waitFor(() => expect(screen.getByTestId('virtual-recents')).toBeTruthy())
  expect(document.querySelectorAll('[data-entered-project-member]')).toHaveLength(0)
})
