import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $hiddenGatewayIds } from '@/store/gateway-visibility'
import { $sidebarShowAllSessions, setSidebarAgentsGrouped } from '@/store/layout'
import type * as ProjectsStore from '@/store/projects'
import { $projectTree } from '@/store/projects'
import { $sessions } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

import oracle from './gateway-project-placement-oracle.json'
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

const row = (id: string, path: string, foreign = false, extra = {}) =>
  makeSessionInfo({
    id,
    title: `ROW ${foreign ? 'studio' : 'local'} ${id}`,
    cwd: path,
    git_repo_root: path,
    connection_id: foreign ? 'studio' : undefined,
    last_active: 1000,
    message_count: 1,
    ...extra
  })

const node = (path: string, rows: ReturnType<typeof row>[]): SidebarProjectTree => ({
  id: path,
  path,
  label: path.split('/').pop()!,
  isAuto: true,
  sessionCount: rows.length,
  lastActive: 5,
  sessionIds: rows.map(r => r.id),
  previewSessions: rows,
  repos: [{ id: path, path, label: 'repo', sessionCount: rows.length, groups: [] }]
})

const titles = (el: Element) =>
  [...el.querySelectorAll('*')]
    .filter(n => !n.children.length && /^ROW /.test(n.textContent ?? ''))
    .map(n => n.textContent!)

const snapshot = () => {
  const blocks: Record<string, string[]> = {}

  for (const el of document.querySelectorAll('[data-project-member]')) {
    const [gateway, , id] = JSON.parse(el.getAttribute('data-project-member')!)
    blocks[JSON.stringify([gateway, id])] = titles(el)
  }

  // Base has no member wrappers. Capture its original groups with the same keys.
  if (!Object.keys(blocks).length) {
    for (const el of document.querySelectorAll('[data-sessions-project]')) {
      const id = el.getAttribute('data-sessions-project')!
      const raw = id.startsWith('[') ? JSON.parse(id) : null
      blocks[JSON.stringify([raw?.[1] ?? null, id])] = titles(el)
    }
  }

  return { blocks, all: screen.queryAllByText(/^ROW /).map(el => el.textContent!) }
}

beforeEach(() => {
  $connectionsRegistry.set({ version: 2, primary: 'local', secureTokenStorage: true, connections: [] })
  $sidebarShowAllSessions.set(false)
  setSidebarAgentsGrouped(true)
  store.fetchProjectSessions.mockReset().mockResolvedValue(null)
})
afterEach(() => {
  cleanup()
  $projectTree.set([])
  $sessions.set([])
  $hiddenGatewayIds.set([])
  $sidebarShowAllSessions.set(true)
  setSidebarAgentsGrouped(false)
})

const shapes = [
  'same-path',
  'same-id-path',
  'same-id-different',
  'deeper-fold',
  'deeper-unfolded',
  'worktree',
  'foreign-only'
] as const

const fixture = (shape: (typeof shapes)[number]) => {
  const root = shape === 'deeper-unfolded' ? '/b/zed' : '/b/app'

  const foreignPath =
    shape === 'foreign-only'
      ? '/x/zeta'
      : shape === 'same-id-different'
        ? '/x/app'
        : shape === 'worktree'
          ? `${root}/.worktrees/foo`
          : shape.startsWith('deeper')
            ? `${root}/deep`
            : root

  const collides = shape.startsWith('same-id')
  const local = [row('A1', root), row('A2', root, false, { last_active: 999 })]

  const foreign = [
    row(collides ? 'A1' : 'S1', foreignPath, true, { last_active: 500 }),
    row(collides ? 'A2' : 'S2', foreignPath, true, { last_active: 499 })
  ]

  const tree = shape === 'foreign-only' ? [] : [node(root, local)]
  const live = shape === 'foreign-only' ? [] : [...local]

  if (shape === 'deeper-fold' || shape === 'worktree') {
    const a = [row('B1', '/a/app', false, { last_active: 100 })]
    tree.unshift(node('/a/app', a))
    live.push(...a)
  }

  tree.push(node('/c/Other', [row('C1', '/c/Other')]))

  if (shape.startsWith('deeper') || shape === 'worktree' || shape === 'foreign-only') {
    live.unshift(row('L1', `${foreignPath}/x`, false, { git_repo_root: undefined, last_active: 2000 }))
  }

  return { root, foreignPath, foreign, tree, live }
}

for (const shape of shapes) {
  for (const hidden of [false, true]) {
    for (const stamped of [false, true]) {
      it(`places each gateway before folding: ${shape}/${hidden}/${stamped}`, async () => {
        const { root, foreignPath, foreign, tree, live } = fixture(shape)

        $projectTree.set(tree)
        $sessions.set([...live.map(r => (stamped ? { ...r, connection_id: 'local' } : r)), ...foreign])
        $hiddenGatewayIds.set(hidden ? ['studio'] : [])
        mount()
        const before = snapshot()
        await act(async () => {
          for (const button of screen
            .queryAllByRole('button')
            .filter(b => /^Show all \d+ sessions$/.test(b.textContent ?? ''))) {
            fireEvent.click(button)
          }
        })
        const result = { before, after: snapshot() }

        if (process.env.HERMES_CAPTURE_GATEWAY_PLACEMENT) {
          process.stdout.write(`GATEWAY_ORACLE ${JSON.stringify({ key: `${shape}/${hidden}/${stamped}`, result })}\n`)

          return
        }

        // Approved presentation-only contract: retain base multiplicity even where base duplicates
        // a foreign row or omits a colliding foreign id. No placement repair belongs in grouping.
        expect(result).toEqual(oracle[`${shape}/${hidden}/${stamped}` as keyof typeof oracle])
      })
    }
  }
}
