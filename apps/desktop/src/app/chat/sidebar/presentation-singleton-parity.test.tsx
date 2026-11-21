import { createHash } from 'node:crypto'

import { cleanup, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import type { SessionInfo } from '@/hermes'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import {
  $pinnedSessionIds,
  $sidebarShowAllSessions,
  $sidebarShowAutomated,
  setSidebarAgentsGrouped
} from '@/store/layout'
import { $projectTree } from '@/store/projects'
import type * as ProjectsStore from '@/store/projects'
import { $sessions } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

import oracle from './presentation-singleton-parity.json'
import type { SidebarProjectTree } from './projects/workspace-groups'

import { ChatSidebar } from './index'

vi.mock('./session-row', () => ({
  SidebarSessionRow: ({ session }: { session: SessionInfo }) => (
    <span data-parity-row={session.id}>{session.title}</span>
  )
}))
vi.mock('@/store/projects', async original => ({
  ...(await original<typeof ProjectsStore>()),
  fetchProjectSessions: vi.fn(async () => null),
  projectProfile: () => 'default'
}))
const noop = vi.fn()
let seed = 0x5a17

const random = () => {
  seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0

  return seed
}

beforeEach(() => {
  $connectionsRegistry.set({ version: 2, primary: 'local', secureTokenStorage: true, connections: [] })
  setSidebarAgentsGrouped(true)
  $sidebarShowAutomated.set(false)
  $sidebarShowAllSessions.set(false)
})
afterEach(() => {
  cleanup()
  $projectTree.set([])
  $sessions.set([])
  $pinnedSessionIds.set([])
  setSidebarAgentsGrouped(false)
  $sidebarShowAllSessions.set(true)
})

for (let cell = 0; cell < 24; cell++) {
  it(`byte-identical singleton DOM, seeded random tree ${cell}`, () => {
    seed = 0x5a17 + cell
    const tree: SidebarProjectTree[] = []
    const live: SessionInfo[] = []
    const count = 1 + (random() % 7)

    for (let project = 0; project < count; project++) {
      const path = `/random/p${cell}-${project}`

      const rows = Array.from({ length: random() % 7 }, (_, row) =>
        makeSessionInfo({
          id: `c${cell}p${project}r${row}`,
          title: `PARITY ${cell}/${project}/${row}`,
          cwd: path,
          git_repo_root: path,
          message_count: 1,
          last_active: 1000 - row,
          automated: random() % 6 === 0
        })
      )

      const foreign = random() % 3 === 0
      live.push(...rows.map(row => (foreign ? { ...row, connection_id: 'studio' } : row)))

      if (!foreign) {
        tree.push({
          id: path,
          path,
          label: `p${cell}-${project}`,
          isAuto: random() % 2 === 0,
          repos: [{ id: path, path, label: 'repo', sessionCount: rows.length, groups: [] }],
          sessionCount: rows.length,
          sessionIds: rows.map(row => row.id),
          previewSessions: rows.slice(0, 3)
        })
      }
    }

    $sidebarShowAllSessions.set(cell % 2 === 0)
    $pinnedSessionIds.set(live.filter((_, index) => index % 7 === 0).map(row => row.id))
    $projectTree.set(tree)
    $sessions.set(live)

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
            onRetrySessions={noop}
            onTriggerCronJob={async () => {}}
          />
        </SidebarProvider>
      </MemoryRouter>
    )

    const html = container.innerHTML

    if (process.env.HERMES_CAPTURE_SINGLETON_PARITY) {
      process.stdout.write(`SINGLETON_PARITY ${JSON.stringify({ key: String(cell), result: html })}\n`)
    } else {
      expect(createHash('sha256').update(html).digest('hex')).toBe(oracle[String(cell) as keyof typeof oracle])
    }
  })
}
