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

it('counts every Background card colour on rows and the header without changing Pinned marks', () => {
  $sessions.set([
    owner('kanban_owner_terminal', { kanban_active: 7, kanban_parked: 5 }),
    owner('kanban_owner_running', { kanban_active: 1 }),
    owner('ordinary', { kanban_active: 7, kanban_parked: 5 })
  ])
  mount()
  const background = section('Background')
  const row = (title: string) => within(background).getByText(title).closest('.group.row-hover') as HTMLElement
  const squares = (element: HTMLElement) => within(element).getByTestId('session-row-card-squares')
  const terminal = squares(row('Owner kanban_owner_terminal'))
  expect(terminal.querySelector('[data-card-colour="grey"]')?.textContent).toBe('5')
  expect(terminal.querySelector('[data-card-colour="blue"]')?.textContent).toBe('2')
  const running = squares(row('Owner kanban_owner_running'))
  expect(running.querySelector('[data-card-colour="blue"]')?.textContent).toBe('1')
  const header = within(background).getByRole('button', { expanded: true })
  expect(within(header).getByText('2')).toBeTruthy()
  const aggregate = squares(header)
  expect(aggregate.querySelector('[data-card-colour="grey"]')?.textContent).toBe('5')
  expect(aggregate.querySelector('[data-card-colour="blue"]')?.textContent).toBe('3')
  expect([...aggregate.querySelectorAll('[data-card-colour]')].map(el => el.getAttribute('data-card-colour'))).toEqual([
    'blue',
    'grey'
  ])
  expect(aggregate.getAttribute('aria-label')).toBe('Running 3 · Waiting 5')
  // Non-Background rows retain main's timer policy: no blue or grey trailing squares.
  expect(within(section('Pinned')).queryByTestId('session-row-card-squares')).toBeNull()
  act(() =>
    $sessions.set([
      owner('kanban_owner_terminal', { kanban_active: 9, kanban_parked: 6 }),
      owner('kanban_owner_running', { kanban_active: 1 }),
      owner('ordinary', { kanban_active: 7, kanban_parked: 5 })
    ])
  )
  expect(squares(header).getAttribute('aria-label')).toBe('Running 4 · Waiting 6')
  fireEvent.click(header)
  expect(within(background).queryByText('Owner kanban_owner_terminal')).toBeNull()
  expect(squares(header).getAttribute('aria-label')).toBe('Running 4 · Waiting 6')
  fireEvent.click(header)
})

it('keeps elapsed timers beside Background counts and Pinned single squares unnumbered', () => {
  const fields = {
    kanban_active: 7,
    kanban_parked: 5,
    kanban_blocked: 1,
    kanban_timed_cards: [{ id: 'card', title: 'Running card', state: 'running', since: Date.now() / 1000 - 45 }]
  }

  $sessions.set([owner('kanban_owner_timed', fields), owner('ordinary', fields)])
  mount()
  const background = section('Background')
  const row = within(background).getByText('Owner kanban_owner_timed').closest('.group.row-hover') as HTMLElement
  const marks = within(row).getByTestId('session-row-card-squares')
  expect(marks.querySelector('[data-card-colour="blue"]')?.textContent).toBe('2')
  expect(marks.querySelector('[data-card-colour="grey"]')?.textContent).toBe('5')
  expect(within(row).getByTestId('session-row-card-timers')).toBeTruthy()
  const pinned = within(section('Pinned')).getByTestId('session-row-card-squares')
  expect(pinned.textContent).toBe('')
  expect(pinned.querySelectorAll('[data-card-colour]')).toHaveLength(1)
  expect(pinned.querySelector('[data-card-colour="red"]')).toBeTruthy()
})

it('aggregates every colour in urgency order and omits zero totals', () => {
  const fields = {
    kanban_active: 12,
    kanban_parked: 5,
    kanban_held: 2,
    kanban_stalled: 1,
    kanban_blocked: 3,
    kanban_attention: 4,
    kanban_unknown: true,
    kanban_unseen: 6
  }

  $sessions.set([owner('kanban_owner_one', fields), owner('kanban_owner_two', fields)])
  mount()
  const header = within(section('Background')).getByRole('button', { expanded: true })
  const marks = within(header).getByTestId('session-row-card-squares')
  expect(
    [...marks.querySelectorAll('[data-card-colour]')].map(el => [el.getAttribute('data-card-colour'), el.textContent])
  ).toEqual([
    ['red', '6'],
    ['pink', '4'],
    ['green', '2'],
    ['amber', '8'],
    ['blue', '10'],
    ['grey', '10'],
    ['violet', '12']
  ])
  act(() => $sessions.set([owner('kanban_owner_one', { kanban_active: 1 })]))
  expect(
    [...within(header).getByTestId('session-row-card-squares').querySelectorAll('[data-card-colour]')].map(el => [
      el.getAttribute('data-card-colour'),
      el.textContent
    ])
  ).toEqual([['blue', '1']])
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
