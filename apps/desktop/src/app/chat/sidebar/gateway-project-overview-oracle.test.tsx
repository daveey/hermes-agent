import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import {
  $dismissedAutoProjectIds,
  $pinnedSessionIds,
  $sidebarShowAllSessions,
  $sidebarShowAutomated,
  setSidebarAgentsGrouped
} from '@/store/layout'
import type * as ProjectsStore from '@/store/projects'
import { $projectTree } from '@/store/projects'
import { $sessions } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

// Captured through mounted ChatSidebar at merge-base 211900189c. The same
// generated harness must preserve every unfolded offer, read and revealed row.
import oracle from './gateway-project-overview-oracle.json'
import type { SidebarProjectTree } from './projects/workspace-groups'

import { ChatSidebar } from './index'
const store = vi.hoisted(() => ({ fetchProjectSessions: vi.fn(), projectProfile: vi.fn(() => 'default') }))
vi.mock('@/store/projects', async importOriginal => ({ ...(await importOriginal<typeof ProjectsStore>()), ...store }))
const noop = vi.fn()

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

const row = (id: string, path: string, extra = {}) =>
  makeSessionInfo({ id, title: `ROW ${id}`, cwd: path, git_repo_root: path, message_count: 1, ...extra })

const node = (path: string, rows: ReturnType<typeof row>[], count = rows.length): SidebarProjectTree => ({
  id: path,
  path,
  label: path.split('/').pop()!,
  isAuto: true,
  sessionCount: count,
  lastActive: 5,
  previewSessions: rows.slice(0, 3),
  sessionIds: rows.map(r => r.id),
  repos: [{ id: path, path, label: 'repo', sessionCount: count, groups: [] }]
})

const offers = () => screen.queryAllByRole('button').filter(b => /^Show all \d+ sessions$/.test(b.textContent ?? ''))

const snapshot = () => ({
  offers: offers().map(b => b.textContent),
  rows: screen
    .queryAllByText(/^ROW /)
    .map(r => r.textContent)
    .sort(),
  calls: store.fetchProjectSessions.mock.calls.map(c => c[0])
})

beforeEach(() => {
  $connectionsRegistry.set({ version: 2, primary: 'local', secureTokenStorage: true, connections: [] })
  $sidebarShowAutomated.set(false)
  $sidebarShowAllSessions.set(false)
  setSidebarAgentsGrouped(true)
  store.fetchProjectSessions.mockReset()
})
afterEach(() => {
  cleanup()
  $projectTree.set([])
  $sessions.set([])
  $pinnedSessionIds.set([])
  $dismissedAutoProjectIds.set([])
  setSidebarAgentsGrouped(false)
  $sidebarShowAllSessions.set(true)
})

const cells = ['local', 'foreign', 'gw'].flatMap(topology =>
  ['preview', 'count-only'].flatMap(shape =>
    ['none', 'pinned', 'automated'].flatMap(hidden =>
      ['null', 'partial', 'complete'].map(response => ({ topology, shape, hidden, response }))
    )
  )
)

describe('unfolded overview base oracle', () => {
  for (const cell of cells) {
    const key = Object.values(cell).join('-')
    it(key, async () => {
      const { topology, shape, hidden, response } = cell
      const other = [row('C1', '/c/Other')]

      const all = Array.from({ length: 5 }, (_, i) =>
        row(`R${i + 1}`, '/w/App', {
          last_active: 100 - i,
          ...(topology === 'foreign' ? { connection_id: 'studio' } : {}),
          automated: hidden === 'automated' && i === 4
        })
      )

      const rows = shape === 'preview' ? all : []
      const project = node('/w/App', rows, 5)

      if (topology === 'gw') {
        project.id = 'gw:studio:/w/App'
        project.path = null
        project.sessionIds = undefined
      }

      $projectTree.set([node('/c/Other', other), ...(topology === 'foreign' ? [] : [project])])
      $sessions.set([...other, ...(topology === 'gw' ? [] : rows)])

      if (hidden === 'pinned') {
        $pinnedSessionIds.set(['R5'])
      }

      store.fetchProjectSessions.mockResolvedValue(
        response === 'null'
          ? null
          : {
              ...project,
              repos: [
                {
                  ...project.repos[0],
                  groups: [
                    {
                      id: '/w/App',
                      path: '/w/App',
                      label: 'repo',
                      sessions: response === 'partial' ? all.slice(0, 2) : all
                    }
                  ]
                }
              ]
            }
      )
      mount()
      const before = snapshot()
      await act(async () => {
        for (const button of offers()) {
          fireEvent.click(button)
        }
      })
      const result = { before, after: snapshot() }
      expect(result).toEqual(oracle[key as keyof typeof oracle])
    })
  }
})
