import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
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
import { $sessions } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

import { ProjectContextMenu, ProjectMenu } from './projects/project-menu'
import type { SidebarProjectTree } from './projects/workspace-groups'

import { ChatSidebar } from './index'

const store = vi.hoisted(() => ({ fetchProjectSessions: vi.fn(), projectProfile: vi.fn(() => 'default') }))
vi.mock('@/store/projects', async original => ({ ...(await original<typeof ProjectsStore>()), ...store }))
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

// An explicit (user-created) window-gateway project: isAuto is unset.
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
})

const labels = () =>
  [...document.querySelectorAll('[aria-label]')]
    .map(el => el.getAttribute('aria-label')!)
    .filter(label => /app|project|menu|Studio|Actions/i.test(label))

const openMenu = (trigger: HTMLElement) => {
  fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.click(trigger)
}

const menuItems = () => screen.getAllByRole('menuitem').map(el => el.textContent)

for (const entered of [false, true]) {
  for (const context of [false, true]) {
    it(`preserves base explicit action items: entered=${entered}, context=${context}`, async () => {
      const control = render(
        context ? (
          <ProjectContextMenu isActive={false} project={base}>
            <div data-testid="control">app</div>
          </ProjectContextMenu>
        ) : (
          <ProjectMenu isActive={false} project={base} />
        )
      )

      if (context) {fireEvent.contextMenu(screen.getByTestId('control'))}
      else {openMenu(screen.getByRole('button', { name: 'Actions' }))}

      await screen.findByRole('menuitem', { name: /^Rename/ })
      const expected = menuItems()
      expect(expected.length).toBeGreaterThanOrEqual(7)
      control.unmount()
      mount()

      if (entered)
        {await act(async () => {
          $projectScope.set('local-app')
        })}

      await act(async () => {})

      const member = document.querySelector(
        entered ? '[data-entered-project-member="local-app"]' : '[data-sessions-project="local-app"]'
      ) as HTMLElement

      if (context) {fireEvent.contextMenu(member.querySelector('[data-slot="context-menu-trigger"]')!)}
      else {openMenu(within(member).getByRole('button', { name: 'Actions' }))}

      await screen.findByRole('menuitem', { name: /^Rename/ })
      expect(menuItems()).toEqual(expected)
    })
  }
}

it('persisted foreign dismissal leaves the never-dismissed explicit member visible', async () => {
  $dismissedAutoProjectIds.set([remoteId])
  mount()
  await act(async () => {})

  // The window gateway's own explicit project was never dismissed: its header must remain.
  expect(labels().some(label => /^(Enter|Open|Show|Hide).*app/i.test(label))).toBe(true)
})

it('explicit member keeps its base Actions menu in overview and entered views', async () => {
  mount()
  await act(async () => {})
  const member = document.querySelector('[data-sessions-project="local-app"]') as HTMLElement
  expect(within(member).getByRole('button', { name: 'Actions' })).toBeTruthy()
  await act(async () => {
    $projectScope.set('local-app')
  })
  const entered = document.querySelector('[data-entered-project-member="local-app"]') as HTMLElement
  expect(within(entered).getByRole('button', { name: 'Actions' })).toBeTruthy()
})

it('group hide dismisses only auto members and leaves the explicit member visible', async () => {
  mount()
  await act(async () => {})
  const group = document.querySelector('[data-presentation-group]') as HTMLElement
  const trigger = within(group).getAllByRole('button', { name: 'Actions' })[0]
  fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.click(trigger)
  await act(async () => {})
  fireEvent.click(screen.getByRole('menuitem', { name: /Hide from sidebar/i }))
  await act(async () => {})
  expect($dismissedAutoProjectIds.get()).toEqual([remoteId])
  expect(document.querySelector('[data-sessions-project="local-app"]')).not.toBeNull()
})
