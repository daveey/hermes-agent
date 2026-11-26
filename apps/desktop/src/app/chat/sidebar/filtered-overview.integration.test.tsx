// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { registry } from '@/contrib/registry'
import {
  $pinnedSessionIds,
  $sidebarShowAllSessions,
  $sidebarStatusFilter,
  resetSidebarView,
  setSidebarAgentsGrouped
} from '@/store/layout'
import { $projectScope, ALL_PROJECTS } from '@/store/project-scope'
import { $projectTree, $projectTreeLoading } from '@/store/projects'
import { $sessions, $sessionsLoading } from '@/store/session'
import { $sessionDotStateById } from '@/store/session-dot-state'
import { SESSION_FILTER_AREA } from '@/store/session-filter'
import { $removedSessionIds } from '@/store/session-removal'
import { makeSessionInfo } from '@/test/session-info'

import type { SidebarProjectTree } from './projects'

import { ChatSidebar } from './index'

vi.mock('@/themes/context', () => ({ useTheme: () => ({ resolvedMode: 'light', setMode: vi.fn() }) }))

const noop = () => {}

const rows = Array.from({ length: 5 }, (_, i) =>
  makeSessionInfo({
    id: `a-${i}`,
    title: `A chat ${i}`,
    cwd: '/a',
    git_repo_root: '/a',
    profile: 'default',
    last_active: 10 - i,
    started_at: 1,
    message_count: 2,
    unread: true
  })
)

const other = makeSessionInfo({ id: 'b', title: 'B chat', cwd: '/b', git_repo_root: '/b', profile: 'default' })
const home = makeSessionInfo({ id: 'home', title: 'Home chat', profile: 'default' })

const node = (id: string, label: string, sessions: typeof rows): SidebarProjectTree => ({
  id,
  label,
  path: id,
  repos: [],
  sessionCount: sessions.length,
  sessionIds: sessions.map(row => row.id),
  previewSessions: sessions.slice(0, 3)
})

const tree: SidebarProjectTree[] = [
  node('/a', 'Project A', rows),
  node('/b', 'Project B', [other]),
  { ...node('__no_project__', 'Home', [home]), path: null, isNoProject: true }
]

function mount() {
  return render(
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
}

const projectRows = () =>
  [...document.querySelectorAll('[data-sessions-project]')]
    .map(row => row.getAttribute('data-sessions-project'))
    .filter(Boolean)

beforeEach(() => {
  resetSidebarView()
  setSidebarAgentsGrouped(true)
  $sidebarShowAllSessions.set(false)
  $projectScope.set(ALL_PROJECTS)
  $pinnedSessionIds.set([])
  $removedSessionIds.set(new Set())
  $sessionsLoading.set(false)
  $projectTreeLoading.set(false)
  $projectTree.set(tree)
  $sessions.set([...rows, other, home].map(row => ({ ...row, kanban_observed_at: Date.now() / 1000 })))
})
afterEach(() => {
  cleanup()
  resetSidebarView()
  $projectScope.set(ALL_PROJECTS)
  $projectTree.set([])
  $sessions.set([])
  $removedSessionIds.set(new Set())
})

it('preserves all project rows and three previews when no filter narrows', () => {
  mount()
  expect(projectRows()).toEqual(expect.arrayContaining(['/a', '/b', '__no_project__']))
  expect(screen.getByText('A chat 2')).toBeTruthy()
  expect(screen.queryByText('A chat 3')).toBeNull()
  expect(screen.getByText('B chat')).toBeTruthy()
})

it('shows a plugin match beyond the snapshot preview and hides empty projects including Home', () => {
  const dispose = registry.register({
    area: SESSION_FILTER_AREA,
    id: 'one-match',
    data: { match: (row: { id: string }) => row.id === 'a-4' }
  })

  try {
    mount()
    expect(projectRows()).toEqual(['/a'])
    expect(screen.getByText('A chat 4')).toBeTruthy()
    expect(screen.queryByText('A chat 0')).toBeNull()
    expect(screen.queryByText('Project B')).toBeNull()
    expect(screen.queryByText('Home')).toBeNull()
  } finally {
    act(dispose)
  }
})

it('uncaps all matching previews and Reset clears the plugin through the real menu', async () => {
  let dispose = noop
  const onClear = vi.fn(() => dispose())
  dispose = registry.register({
    area: SESSION_FILTER_AREA,
    id: 'all-a',
    data: { match: (row: { id: string }) => row.id.startsWith('a-'), onClear }
  })

  try {
    mount()
    expect(projectRows()).toEqual(['/a'])

    for (const row of rows) {
      expect(screen.getByText(row.title!)).toBeTruthy()
    }
    fireEvent.keyDown(screen.getByRole('button', { name: 'Filters' }), { key: 'ArrowDown' })
    fireEvent.click(await screen.findByText('Reset to defaults'))
    expect(onClear).toHaveBeenCalledOnce()
    expect(screen.getByText('B chat')).toBeTruthy()
    act(() => {
      setSidebarAgentsGrouped(true)
      $sidebarShowAllSessions.set(false)
    })
    expect(projectRows()).toEqual(expect.arrayContaining(['/a', '/b', '__no_project__']))
    expect(screen.queryByText('A chat 3')).toBeNull()
  } finally {
    act(dispose)
  }
})

it('narrows with a core status filter and menu Reset restores the capped overview', async () => {
  mount()
  act(() => $sidebarStatusFilter.set(['unread']))
  expect($sessionDotStateById.get()).toMatchObject({ 'a-0': 'unread' })
  expect(projectRows()).toEqual(['/a'])

  for (const row of rows) {
    expect(screen.getByText(row.title!)).toBeTruthy()
  }
  expect(screen.queryByText('B chat')).toBeNull()
  fireEvent.keyDown(screen.getByRole('button', { name: 'Filters' }), { key: 'ArrowDown' })
  fireEvent.click(await screen.findByText('Reset to defaults'))
  expect($sidebarStatusFilter.get()).toEqual([])
  expect(screen.getByText('B chat')).toBeTruthy()
  act(() => {
    setSidebarAgentsGrouped(true)
    $sidebarShowAllSessions.set(false)
  })
  expect(projectRows()).toEqual(expect.arrayContaining(['/a', '/b', '__no_project__']))
  expect(screen.getByText('A chat 2')).toBeTruthy()
  expect(screen.queryByText('A chat 3')).toBeNull()
})

it('does not retain a project whose only match was optimistically removed', () => {
  $removedSessionIds.set(new Set(['a-4']))

  const dispose = registry.register({
    area: SESSION_FILTER_AREA,
    id: 'removed-match',
    data: { match: (row: { id: string }) => row.id === 'a-4' }
  })

  try {
    mount()
    expect(projectRows()).toEqual([])
    expect(screen.queryByText('A chat 4')).toBeNull()
    expect(screen.getAllByText('No sessions match these filters').length).toBeGreaterThan(0)
  } finally {
    act(dispose)
  }
})

it('keeps the entered project even when no loaded row matches', () => {
  $projectScope.set('/b')
  const dispose = registry.register({ area: SESSION_FILTER_AREA, id: 'entered-miss', data: { match: () => false } })

  try {
    mount()
    expect($projectScope.get()).toBe('/b')
    expect(screen.getByText('Project B')).toBeTruthy()
    expect(screen.queryByText('Project A')).toBeNull()
  } finally {
    act(dispose)
  }
})
