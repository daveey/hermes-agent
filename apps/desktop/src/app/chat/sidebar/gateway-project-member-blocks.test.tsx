import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

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

import memberOracle from './gateway-project-member-oracle.json'
import type { SidebarProjectTree } from './projects/workspace-groups'

import { ChatSidebar } from './index'

const store = vi.hoisted(() => ({ fetchProjectSessions: vi.fn(), projectProfile: vi.fn(() => 'default') }))
vi.mock('@/store/projects', async original => ({ ...(await original<typeof ProjectsStore>()), ...store }))
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

const offers = () =>
  screen.queryAllByRole('button').filter(button => /^Show all \d+ sessions$/.test(button.textContent ?? ''))

const snapshot = () => ({
  rows: screen
    .queryAllByText(/^MEMBER /)
    .filter(el => el.closest('[data-sessions-project]'))
    .map(el => el.textContent)
    .sort(),
  buttons: offers()
    .map(el => el.textContent)
    .sort(),
  calls: store.fetchProjectSessions.mock.calls.map(call => call[0]).sort()
})

const node = (path: string, rows: ReturnType<typeof makeSessionInfo>[], count: number): SidebarProjectTree => ({
  id: path,
  path,
  label: 'App',
  isAuto: true,
  sessionCount: count,
  lastActive: rows[0]?.last_active ?? 0,
  previewSessions: rows.slice(0, 3),
  sessionIds: rows.map(row => row.id),
  repos: [{ id: path, path, label: 'repo', sessionCount: count, groups: [] }]
})

beforeEach(() => {
  $connectionsRegistry.set({ version: 2, primary: 'local', secureTokenStorage: true, connections: [] })
  $sidebarShowAutomated.set(false)
  setSidebarAgentsGrouped(true)
})
afterEach(() => {
  cleanup()
  $projectTree.set([])
  $sessions.set([])
  $pinnedSessionIds.set([])
  $dismissedAutoProjectIds.set([])
  $sidebarShowAllSessions.set(true)
  setSidebarAgentsGrouped(false)
})

const splits = [
  [2, 2],
  [3, 3],
  [1, 3],
  [3, 1],
  [1, 1, 1, 1],
  [3, 4],
  [4, 2],
  [5, 5, 5]
]

for (const counts of splits) {
  for (const showAll of [false, true]) {
    for (const hidden of ['none', 'pinned', 'automated', 'count-only']) {
      for (const response of ['null', 'partial', 'complete']) {
        it(`${counts.join('+')} showAll=${showAll} ${hidden} ${response}: every member equals its unfolded base block`, async () => {
          $sidebarShowAllSessions.set(showAll)

          const rows = counts.map((count, member) =>
            Array.from({ length: hidden === 'count-only' && member === 1 ? 0 : count }, (_, index) =>
              makeSessionInfo({
                id: `M${member}-${index}`,
                title: `MEMBER ${member}-${index}`,
                cwd: `/m${member}/App`,
                git_repo_root: `/m${member}/App`,
                message_count: 1,
                last_active: 1000 - member * 100 - index,
                automated: hidden === 'automated' && member === 1 && index === count - 1
              })
            )
          )

          const nodes = rows.map((items, member) => node(`/m${member}/App`, items, counts[member]))

          if (hidden === 'pinned') {
            $pinnedSessionIds.set([`M1-${counts[1] - 1}`])
          }

          const configure = () =>
            store.fetchProjectSessions.mockReset().mockImplementation(async (id: string) => {
              const original = nodes.find(item => item.id === id)!

              if (response === 'null') {
                return null
              }

              const sessions = rows[nodes.indexOf(original)]

              return {
                ...original,
                repos: [
                  {
                    ...original.repos[0],
                    groups: [
                      {
                        id,
                        path: id,
                        label: 'main',
                        sessions: response === 'partial' ? sessions.slice(0, 2) : sessions
                      }
                    ]
                  }
                ]
              }
            })

          const inspect = async (selected: SidebarProjectTree[]) => {
            configure()
            $projectTree.set(selected)
            $sessions.set(rows.flat())
            mount()
            const before = snapshot()
            await act(async () => {
              for (const button of offers()) {
                fireEvent.click(button)
              }
            })
            const after = snapshot()
            cleanup()

            return { before, after }
          }

          const originals: Awaited<ReturnType<typeof inspect>>[] = []

          for (const original of nodes) {
            originals.push(await inspect([original]))
          }

          const combine = (phase: 'before' | 'after') => ({
            rows: originals.flatMap(result => result[phase].rows).sort(),
            buttons: originals.flatMap(result => result[phase].buttons).sort(),
            calls: originals.flatMap(result => result[phase].calls).sort()
          })

          // Capture mode exercises this same harness against the real merge-base.
          if (import.meta.env.VITE_R8_BASE_CAPTURE) {
            process.stdout.write(
              `R8_BASE ${JSON.stringify({ counts, showAll, hidden, response, before: combine('before'), after: combine('after') })}\n`
            )

            return
          }

          const captured =
            memberOracle[`${counts.join('+')}|${showAll}|${hidden}|${response}` as keyof typeof memberOracle]

          expect({ before: combine('before'), after: combine('after') }).toEqual(captured)
          const folded = await inspect(nodes)
          expect(folded).toEqual(captured)
        })
      }
    }
  }
}
