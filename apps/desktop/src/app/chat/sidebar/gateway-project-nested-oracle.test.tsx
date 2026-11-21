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

import oracle from './gateway-project-nested-oracle.json'
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

const mk = (key: string, path: string, n: number, extra = {}) =>
  Array.from({ length: n }, (_, i) => row(`${key}${i + 1}`, path, { last_active: 1000 - i, ...extra }))

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

const full = (path: string, rows: ReturnType<typeof row>[]): SidebarProjectTree => ({
  ...node(path, rows),
  repos: [
    {
      id: path,
      path,
      label: 'repo',
      sessionCount: rows.length,
      groups: [{ id: path, path, label: 'main', isMain: true, sessions: rows }]
    }
  ]
})

beforeEach(() => {
  $connectionsRegistry.set({ version: 2, primary: 'local', secureTokenStorage: true, connections: [] })
  $sidebarShowAutomated.set(false)
  $sidebarShowAllSessions.set(false)
  setSidebarAgentsGrouped(true)
  store.fetchProjectSessions.mockReset().mockResolvedValue(null)
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

// Recorded from the merge-base's independent, unfolded overview blocks.
const offers = () => screen.queryAllByRole('button').filter(b => /^Show all \d+ sessions$/.test(b.textContent ?? ''))

const snapshot = () => {
  const groups: Record<string, { rows: Record<string, (string | null)[]>; buttons: (string | null)[] }> = {}

  for (const el of document.querySelectorAll('[data-sessions-project]')) {
    const id = el.getAttribute('data-sessions-project')!
    const path = id.startsWith('[') ? (JSON.parse(id).at(-1) as string) : id
    const name = path.split('/').pop()!.toLowerCase()
    const group = (groups[name] ??= { rows: {}, buttons: [] })

    for (const row of [...el.querySelectorAll('*')].filter(
      n => n.children.length === 0 && /^ROW /.test(n.textContent ?? '')
    )) {
      const member = row.textContent!.replace(/\d+$/, '')

      ;(group.rows[member] ??= []).push(row.textContent)
    }

    group.buttons.push(
      ...[...el.querySelectorAll('button')]
        .filter(b => /^Show all \d+ sessions$/.test(b.textContent ?? ''))
        .map(b => b.textContent)
    )
  }

  for (const group of Object.values(groups)) {
    group.buttons.sort()
  }

  return {
    groups,
    // Count the WHOLE sidebar, including Pinned, not just the expected project.
    counts: Object.fromEntries(
      [...new Set(screen.queryAllByText(/^ROW /).map(el => el.textContent))].map(title => [
        title,
        screen.queryAllByText(title!).length
      ])
    ),
    // Foreign owner routing is an intentional PR change; compare ids to base,
    // and assert the route independently below rather than pinning base's missing owner.
    calls: store.fetchProjectSessions.mock.calls.map(c => c[0])
  }
}

const cases = ['outside-local', 'outside-foreign', 'nested-local', 'nested-foreign'] as const

for (const shape of cases) {
  for (const hidden of ['none', 'pinned', 'automated'] as const) {
    for (const showAll of [false, true]) {
      const key = `${shape}/${hidden}/${showAll}`
      it(`preserves full-tree ownership ${key}`, async () => {
        const nested = shape.startsWith('nested')
        const foreign = shape.endsWith('foreign')
        const root = foreign ? '/x/app' : '/a/app'
        const nestedPath = `${root}/${nested ? 'app' : 'tools'}`
        const a = mk('A', root, 4, { ...(foreign ? { connection_id: 'studio' } : {}), last_active: 1000 })

        const t = mk('T', nestedPath, 2, {
          ...(foreign ? { connection_id: 'studio' } : {}),
          last_active: 800,
          automated: hidden === 'automated'
        })

        const b = mk('B', '/b/app', 1, { last_active: 500 })
        const outside = mk('Z', '/z/Other', 1, { last_active: 300 })
        $sidebarShowAllSessions.set(showAll)
        $pinnedSessionIds.set(hidden === 'pinned' ? ['T1'] : [])
        $projectTree.set([
          ...(foreign ? [] : [node(root, a), node(nestedPath, t)]),
          node('/b/app', b),
          node('/z/Other', outside)
        ])
        $sessions.set([...a, ...t, ...b, ...outside])
        store.fetchProjectSessions.mockImplementation(async (id: string) => (id === root ? full(root, a) : null))
        mount()

        if (hidden === 'automated') {
          expect(screen.queryAllByText(/^ROW T/)).toHaveLength(0)
        }

        if (hidden === 'pinned') {
          expect(screen.getAllByText('ROW T1')).toHaveLength(1)
        }

        const before = snapshot()
        await act(async () => {
          for (const button of offers()) {
            fireEvent.click(button)
          }
        })
        const result = { before, after: snapshot() }

        if (process.env.HERMES_CAPTURE_NESTED_ORACLE) {
          process.stdout.write(`NESTED_ORACLE ${JSON.stringify({ key, result })}\n`)
        } else {
          expect(result).toEqual(oracle[key as keyof typeof oracle])

          // Presentation grouping preserves base fetch options, including its foreign-routing limitation.
          for (const call of store.fetchProjectSessions.mock.calls) {
            expect(call[1]).toEqual({ supersedable: false })
          }

          for (const count of Object.values(result.before.counts)) {
            expect(count).toBe(1)
          }

          for (const count of Object.values(result.after.counts)) {
            expect(count).toBe(1)
          }
        }
      })
    }
  }
}
