import { createHash } from 'node:crypto'

import { cleanup, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import type { SessionInfo } from '@/hermes'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $sidebarShowAllSessions, setSidebarAgentsGrouped } from '@/store/layout'
import { $projectScope, ALL_PROJECTS } from '@/store/project-scope'
import { $projectTree } from '@/store/projects'
import type * as ProjectsStore from '@/store/projects'
import { $sessions } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

import oracle from './presentation-entered-singleton-parity.json'
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
beforeEach(() => {
  $connectionsRegistry.set({ version: 2, primary: 'local', secureTokenStorage: true, connections: [] })
  setSidebarAgentsGrouped(true)
  $sidebarShowAllSessions.set(true)
})
afterEach(() => {
  cleanup()
  $projectTree.set([])
  $sessions.set([])
  $projectScope.set(ALL_PROJECTS)
  setSidebarAgentsGrouped(false)
})

it.each([0, 1, 3, 26])('singleton entered DOM equals actual main, %s rows', async count => {
  const path = '/code/app'
  const rows = Array.from({ length: count }, (_, i) =>
    makeSessionInfo({
      id: `E${i}`,
      title: `ENTERED ${i}`,
      cwd: path,
      git_repo_root: path,
      message_count: 1,
      last_active: 1000 - i
    })
  )
  const node: SidebarProjectTree = {
    id: path,
    label: 'app',
    path,
    sessionCount: count,
    sessionIds: rows.map(row => row.id),
    previewSessions: rows.slice(0, 3),
    repos: [
      {
        id: path,
        path,
        label: 'app',
        sessionCount: count,
        groups: [{ id: 'main', label: 'main', path, isMain: true, sessions: rows }]
      }
    ]
  }
  $projectTree.set([node])
  $sessions.set(rows)
  $projectScope.set(path)
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
  const digest = createHash('sha256').update(html).digest('hex')
  if (process.env.HERMES_CAPTURE_ENTERED_PARITY) {
    process.stdout.write(`ENTERED_PARITY ${JSON.stringify({ key: String(count), result: html, digest })}\n`)
  } else {
    expect(digest).toBe(oracle[String(count) as keyof typeof oracle])
  }
})
