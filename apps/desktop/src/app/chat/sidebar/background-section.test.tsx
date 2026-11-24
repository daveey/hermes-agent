import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { $pinnedSessionIds, resetSidebarView, setSidebarAgentsGrouped } from '@/store/layout'
import { $projectTree } from '@/store/projects'
import { $sessions } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

import { ChatSidebar } from './index'

const noop = vi.fn()
const resume = vi.fn()

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
          onResumeSession={resume}
          onRetrySessions={noop}
          onTriggerCronJob={async () => {}}
        />
      </SidebarProvider>
    </MemoryRouter>
  )

const owner = (id: string, extra = {}) =>
  makeSessionInfo({
    id,
    title: `Owner ${id}`,
    pinned: true,
    message_count: 1,
    last_active: 100,
    ...extra
  })

const section = (label: string) => screen.getByText(label).closest('[data-slot="sidebar-group"]') as HTMLElement

beforeEach(() => {
  resetSidebarView()
  $pinnedSessionIds.set([])
  $sessions.set([])
  $projectTree.set([])
  resume.mockClear()
})
afterEach(() => {
  cleanup()
  $sessions.set([])
  $projectTree.set([])
  $pinnedSessionIds.set([])
})

it('moves only owners with open cards below Pinned and excludes all owners from Projects', () => {
  const rows = [
    owner('kanban_owner_runner', { kanban_active: 1, cwd: '/repo', last_active: 200 }),
    owner('kanban_owner_idle', { cwd: '/repo' }),
    owner('compressed-tip', { _lineage_root_id: 'kanban_owner_root', kanban_blocked: 1, cwd: '/repo' }),
    makeSessionInfo({ id: 'normal-pin', title: 'Normal pinned chat', pinned: true, message_count: 1 }),
    makeSessionInfo({ id: 'project-chat', title: 'Project chat', cwd: '/repo', message_count: 1 })
  ]

  $sessions.set(rows)
  setSidebarAgentsGrouped(true)
  $projectTree.set([
    {
      id: '/repo',
      path: '/repo',
      label: 'Repo',
      isAuto: true,
      sessionCount: 4,
      lastActive: 200,
      sessionIds: rows.filter(row => row.cwd).map(row => row.id),
      previewSessions: rows.filter(row => row.cwd),
      repos: [
        {
          id: '/repo',
          path: '/repo',
          label: 'repo',
          sessionCount: 4,
          groups: [{ id: '/repo', path: '/repo', label: 'main', isMain: true, sessions: rows.filter(row => row.cwd) }]
        }
      ]
    }
  ])
  mount()
  const background = section('Background')
  expect(within(background).getByText('2')).toBeTruthy()
  expect(within(section('Pinned')).getByText('Normal pinned chat')).toBeTruthy()
  expect(within(section('Pinned')).queryByText('Owner kanban_owner_runner')).toBeNull()

  for (const title of ['Owner kanban_owner_runner', 'Owner compressed-tip']) {
    expect(screen.getAllByText(title)).toHaveLength(1)
    expect(within(background).getByText(title)).toBeTruthy()
  }

  expect(screen.queryByText('Owner kanban_owner_idle')).toBeNull()
  expect(section('Pinned').nextElementSibling).toBe(background)
  const titles = [...background.querySelectorAll('.group.row-hover')].map(row => row.textContent)
  expect(titles[0]).toContain('Owner kanban_owner_runner')
  fireEvent.click(screen.getByText('Owner compressed-tip'))
  expect(resume).toHaveBeenCalledWith(
    'compressed-tip',
    expect.objectContaining({ _lineage_root_id: 'kanban_owner_root' })
  )
})

it('hides the section when the last open card closes and keeps normal pins', () => {
  const row = owner('kanban_owner_runner', { kanban_parked: 1 })
  $sessions.set([row, owner('ordinary', { title: 'Normal pinned chat' })])
  mount()
  expect(section('Background')).toBeTruthy()
  act(() => $sessions.set([{ ...row, kanban_parked: 0 }, owner('ordinary', { title: 'Normal pinned chat' })]))
  expect(screen.queryByText('Background')).toBeNull()
  expect(screen.queryByText('Owner kanban_owner_runner')).toBeNull()
  expect(screen.getByText('Normal pinned chat')).toBeTruthy()
})

it('persists Background collapse independently of Pinned across remounts', () => {
  $sessions.set([owner('kanban_owner_runner', { kanban_held: 1 })])
  const view = mount()
  fireEvent.click(within(section('Background')).getByRole('button', { expanded: true }))
  expect(within(section('Background')).getByRole('button', { expanded: false })).toBeTruthy()
  expect(within(section('Pinned')).getByRole('button', { expanded: true })).toBeTruthy()
  view.unmount()
  mount()
  const toggle = within(section('Background')).getByRole('button', { expanded: false })
  fireEvent.click(toggle)
  expect(screen.getByText('Owner kanban_owner_runner')).toBeTruthy()
})
