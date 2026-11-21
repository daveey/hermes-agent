import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopConnectionsRegistry } from '@/global'
import type { SessionInfo } from '@/hermes'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { mergeGatewayProjectTrees } from '@/store/gateway-project-merge'
import { $sidebarShowAllSessions } from '@/store/layout'
import { makeSessionInfo } from '@/test/session-info'

import { SidebarSessionsSection } from '../sessions-section'

import { withGatewayProjectRows } from './gateway-project-rows'
import type * as ProjectsModel from './model'
import { NO_PROJECT_ID, overlayLivePreviews, type SidebarProjectTree } from './workspace-groups'

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

const foreign = (session: SessionInfo) =>
  session.connection_id && session.connection_id !== 'local' ? session.connection_id : null

// Disjoint fixture: one name ("app"/"App"/"APP") on three machines, a foreign-only name
// ("infra"/"INFRA") on two, a remote-only project whose rows the live pool omits, plus Home.
const live: SessionInfo[] = [
  row('local-app', { cwd: '/code/app', git_repo_root: '/code/app' }),
  row('lab-app', { connection_id: 'homelab', cwd: '/srv/App', git_repo_root: '/srv/App', last_active: 2 }),
  row('studio-app', { connection_id: 'studio', cwd: '/work/APP', git_repo_root: '/work/APP', last_active: 3 }),
  row('studio-infra', { connection_id: 'studio', cwd: '/work/infra', git_repo_root: '/work/infra', last_active: 4 }),
  row('lab-infra', { connection_id: 'homelab', cwd: '/srv/INFRA', git_repo_root: '/srv/INFRA', last_active: 9 }),
  row('lab-loose', { connection_id: 'homelab' }),
  row('local-loose')
]

const noteRow = row('lab-note', { last_active: 5 })

const base: SidebarProjectTree[] = [
  {
    id: NO_PROJECT_ID,
    isNoProject: true,
    label: 'Home',
    path: null,
    repos: [],
    sessionCount: 1,
    sessionIds: ['local-loose'],
    previewSessions: [live[6]]
  },
  {
    id: '/code/app',
    label: 'app',
    path: '/code/app',
    repos: [],
    sessionCount: 1,
    sessionIds: ['local-app'],
    previewSessions: [live[0]],
    lastActive: 1
  }
]

const buildTree = () => {
  const merged = mergeGatewayProjectTrees(
    base,
    [
      {
        connectionId: 'homelab',
        projects: [
          {
            id: 'p_notes',
            label: 'Notes',
            path: '/srv/notes',
            repos: [],
            sessionCount: 1,
            sessionIds: ['lab-note'],
            previewSessions: [noteRow]
          }
        ]
      }
    ],
    3
  )

  return withGatewayProjectRows(merged, live, foreign, id => id)
}

const registry = {
  primary: 'local',
  version: 1,
  connections: [
    { id: 'local', kind: 'local', label: 'This Mac' },
    { id: 'homelab', kind: 'remote', label: 'Homelab' },
    { id: 'studio', kind: 'remote', label: 'Studio' }
  ]
} as unknown as DesktopConnectionsRegistry

const noop = vi.fn()

let root: HTMLElement

const show = (tree: SidebarProjectTree[], onNewSession = vi.fn(), onReorderProjects = vi.fn()) => {
  const previews = overlayLivePreviews(tree, live, [], 10)

  root = render(
    <SidebarSessionsSection
      activeSessionId={null}
      emptyState={null}
      label="Projects"
      onArchiveSession={noop}
      onDeleteSession={noop}
      onNewSessionInWorkspace={onNewSession}
      onReorderProjects={onReorderProjects}
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

  return { onNewSession, onReorderProjects, previews }
}

beforeEach(() => {
  $sidebarShowAllSessions.set(false)
  $connectionsRegistry.set(registry)
})

afterEach(() => {
  cleanup()
  $connectionsRegistry.set(null)
})

const groupEl = (key: string) => root.querySelector<HTMLElement>(`[data-presentation-group="name:${key}"]`)

describe('overview presentation groups', () => {
  it('shows every row exactly once, with no label suffix and no placement change', () => {
    const tree = buildTree()

    expect(tree.map(node => node.label)).toEqual(['Home', 'app', 'Notes', 'App', 'APP', 'infra', 'INFRA'])
    expect(tree.map(node => node.id)).toEqual(buildTree().map(node => node.id))
    show(tree)

    const shown = screen.getAllByTestId('row').map(el => el.textContent)

    expect([...shown].sort()).toEqual([...live.map(r => r.id), 'lab-note'].sort())
    expect(new Set(shown).size).toBe(shown.length)
  })

  it('renders one shell per same-name group (members present), singletons as plain rows', () => {
    show(buildTree())

    const app = groupEl('app')!
    const infra = groupEl('infra')!

    expect(app).not.toBeNull()
    expect(infra).not.toBeNull()
    expect(groupEl('notes')).toBeNull()
    expect(
      within(app)
        .getAllByTestId('row')
        .map(el => el.textContent)
    ).toEqual(['local-app', 'studio-app', 'lab-app'])
    expect(
      within(infra)
        .getAllByTestId('row')
        .map(el => el.textContent)
    ).toEqual(['lab-infra', 'studio-infra'])
    expect(within(app).getByText('Homelab')).toBeTruthy()
    expect(within(app).getByText('Studio')).toBeTruthy()
    // Singletons keep the old row: its own enter link and its own "+".
    expect(screen.getByRole('button', { name: 'New session in Notes' })).toBeTruthy()
    expect(screen.getAllByRole('button', { name: /^New session in (app|infra)/i })).toHaveLength(2)
  })

  it('header "+" starts on the window primary with no route, or routes the recent foreign primary', () => {
    const { onNewSession } = show(buildTree())

    fireEvent.click(within(groupEl('app')!).getByRole('button', { name: 'New session in app' }))
    expect(onNewSession).toHaveBeenLastCalledWith('/code/app')

    fireEvent.click(within(groupEl('infra')!).getByRole('button', { name: 'New session in INFRA' }))
    expect(onNewSession).toHaveBeenLastCalledWith('/srv/INFRA', { connectionId: 'homelab', profile: 'default' })
  })

  it('keeps Home a plain row and never folds it into a project named Home', () => {
    const tree = buildTree()

    show([...tree, { ...tree[2], id: 'home-named', label: ' home ', sessionIds: [], previewSessions: [] }])
    expect(groupEl('home')).toBeNull()
    expect(screen.getAllByRole('button', { name: /Enter (Home| home )/i })).toHaveLength(2)
  })
})
