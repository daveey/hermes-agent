import { cleanup, fireEvent, render, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { DesktopConnectionsRegistry } from '@/global'
import type { SessionInfo } from '@/hermes'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $sidebarShowAllSessions } from '@/store/layout'
import { $connection } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

import { SidebarSessionsSection } from '../sessions-section'

import { withGatewayProjectRows } from './gateway-project-rows'
import type * as ProjectsModel from './model'
import { overlayLivePreviews, type SidebarProjectTree } from './workspace-groups'

vi.mock('../session-row', () => ({
  SidebarSessionRow: ({ session }: { session: SessionInfo }) => <div data-testid="row">{session.id}</div>
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { cancel: 'Cancel' },
      desktop: {},
      sidebar: {
        dateDivider: {},
        newSessionIn: (label: string) => `New session in ${label}`,
        noSessions: 'No sessions yet',
        projects: {
          autoDiscovered: 'Auto-discovered',
          enter: (label: string) => `Enter ${label}`,
          reorder: (label: string) => `Reorder ${label}`,
          showAllCount: (count: number) => `Show all ${count}`,
          toggle: (label: string, open: boolean) => `${open ? 'Hide' : 'Show'} ${label}`
        },
        showMoreIn: (count: number, label: string) => `Show ${count} more in ${label}`
      },
      profiles: {},
      statusStack: { coding: {} }
    }
  })
}))

vi.mock('./model', async () => ({
  ...(await vi.importActual<typeof ProjectsModel>('./model')),
  latestProjectSessions: () => [],
  useWorkspaceNodeOpen: () => [true, vi.fn()]
}))

vi.mock('./project-menu', () => ({
  ProjectContextMenu: ({ children }: { children: ReactNode }) => children,
  ProjectMenu: () => null
}))

const row = (id: string, extra: Partial<SessionInfo> = {}) => makeSessionInfo({ id, last_active: 1, ...extra })

const registry = {
  primary: 'local',
  version: 1,
  connections: [
    { id: 'local', kind: 'local', label: 'This Mac' },
    { id: 'studio', kind: 'remote', label: 'Studio' }
  ]
} as unknown as DesktopConnectionsRegistry

const noop = vi.fn()

function run(windowId: string, sameOwner = false) {
  // Same resolution as useSessionOwners: bare row = registry primary; foreign = not the window's.
  const foreign = (s: SessionInfo) => {
    const o = (s.connection_id ?? '').trim() || 'local'

    return o !== windowId ? o : null
  }

  const studioRow = row('studio-app', {
    connection_id: sameOwner ? 'local' : 'studio',
    cwd: '/work/app',
    git_repo_root: '/work/app',
    last_active: 3
  })

  const localRow = row('local-app', {
    connection_id: 'local',
    cwd: '/code/app',
    git_repo_root: '/code/app',
    last_active: 5
  })

  const own = windowId === 'studio' ? studioRow : localRow

  const base: SidebarProjectTree[] = [
    {
      id: own.cwd!,
      label: 'app',
      path: own.cwd!,
      repos: [],
      sessionCount: 1,
      sessionIds: [own.id],
      previewSessions: [own],
      lastActive: 3
    }
  ]

  $sidebarShowAllSessions.set(false)
  $connectionsRegistry.set(registry)
  $connection.set({ connectionId: windowId, mode: windowId === 'local' ? 'local' : 'remote' } as never)

  const sameMachine: SidebarProjectTree[] = [localRow, studioRow].map(s => ({
    id: s.cwd!,
    label: 'app',
    path: s.cwd!,
    connectionId: 'local',
    repos: [],
    sessionCount: 1,
    sessionIds: [s.id],
    previewSessions: [s]
  }))

  const tree = withGatewayProjectRows(sameOwner ? sameMachine : base, [studioRow, localRow], foreign)
  const previews = overlayLivePreviews(tree, [studioRow, localRow], [], 10)
  const onNew = vi.fn()

  const root = render(
    <SidebarSessionsSection
      activeSessionId={null}
      emptyState={null}
      label="Projects"
      onArchiveSession={noop}
      onDeleteSession={noop}
      onNewSessionInWorkspace={onNew}
      onResumeSession={noop}
      onToggle={noop}
      onTogglePin={noop}
      onToggleUnread={noop}
      open
      pinned={false}
      projectOverview={tree}
      projectOverviewPreviews={previews}
      sessions={[]}
    />
  ).container

  const g = root.querySelector<HTMLElement>('[data-presentation-group="name:app"]')!
  const rows = [...g.querySelectorAll('[data-testid="row"]')].map(e => e.textContent)
  const heads = [...new Set([...g.querySelectorAll('span.inline-flex > span.truncate')].map(e => e.textContent))]

  fireEvent.click(within(g).getByRole('button', { name: 'New session in app' }))

  const out = {
    windowId,
    tree: tree.map(n => [n.id, n.connectionId ?? null]),
    rows,
    heads,
    newCall: onNew.mock.calls
  }

  return out
}

afterEach(() => {
  cleanup()
  $connectionsRegistry.set(null)
  $connection.set(null)
})

describe('presentation member machine labels', () => {
  it.each(['local', 'studio'])('omits machine headings for same-owner members in the %s window', windowId => {
    const result = run(windowId, true)
    expect(result.heads).toEqual([])
    expect(result.rows.sort()).toEqual(['local-app', 'studio-app'])
  })

  it('control: local window labels each member with its own machine', () => {
    expect(run('local').heads.sort()).toEqual(['Studio', 'This Mac'])
  })

  it('remote window labels each member with its own machine', () => {
    expect(run('studio').heads.sort()).toEqual(['Studio', 'This Mac'])
  })
})
