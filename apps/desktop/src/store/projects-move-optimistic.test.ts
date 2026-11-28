import { atom } from 'nanostores'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { activeGateway } from '@/store/gateway'
import { $activeGatewayProfile, setShowAllProfiles } from '@/store/profile'
import { $sessions, setSessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import {
  $projectOwnerBySessionId,
  $projectTree,
  createProjectAndMoveSession,
  moveSessionToProject,
  refreshProjectTree
} from './projects'
import { reassignTreeSession } from './projects-move-optimistic'

vi.mock('@/i18n', () => ({ translateNow: (key: string) => key }))
vi.mock('@/store/notifications', () => ({ notify: vi.fn() }))
vi.mock('@/lib/desktop-fs', () => ({
  desktopDefaultCwd: vi.fn(),
  isDesktopFsRemoteMode: vi.fn(),
  selectDesktopPaths: vi.fn(),
  writeDesktopFileText: vi.fn()
}))
vi.mock('@/store/gateway', () => ({
  $gateway: atom(null),
  activeGateway: vi.fn(),
  activeGatewayConnectionId: vi.fn(() => null),
  isActivePrimary: vi.fn(() => true),
  ensureActiveGatewayOpen: vi.fn()
}))
vi.mock('@/lib/desktop-git', async importOriginal => ({
  ...((await importOriginal()) as Record<string, unknown>),
  desktopGit: vi.fn()
}))
vi.mock('@/hermes', () => ({
  getHermesConfig: vi.fn(),
  getProfiles: vi.fn(),
  hermesApi: vi.fn(),
  setApiRequestProfile: vi.fn(),
  STARTUP_REQUEST_TIMEOUT_MS: 1000
}))

const row = (id: string, cwd: string): SessionInfo => ({ cwd, id, title: id }) as unknown as SessionInfo

function treeWith(owner: 'p_dest' | 'p_src'): SidebarProjectTree[] {
  const s1 = row('s1', owner === 'p_src' ? '/repos/src' : '/repos/dest')

  return [
    {
      id: 'p_src',
      label: 'Src',
      path: '/repos/src',
      previewSessions: owner === 'p_src' ? [s1] : [],
      repos: [],
      sessionCount: owner === 'p_src' ? 1 : 0,
      sessionIds: owner === 'p_src' ? ['s1'] : []
    },
    {
      id: 'p_dest',
      label: 'Dest',
      path: '/repos/dest',
      previewSessions: owner === 'p_dest' ? [s1] : [],
      repos: [],
      sessionCount: owner === 'p_dest' ? 1 : 0,
      sessionIds: owner === 'p_dest' ? ['s1'] : []
    }
  ]
}

describe('moveSessionToProject shows the row in the destination at once', () => {
  let releaseMove: () => void
  let movedOnBackend: boolean
  let failMove: boolean

  beforeEach(() => {
    vi.clearAllMocks()
    setShowAllProfiles(false)
    $activeGatewayProfile.set('default')
    movedOnBackend = false
    failMove = false
    $projectTree.set(treeWith('p_src'))
    setSessions(() => [row('s1', '/repos/src')])

    const request = vi.fn(async (method: string) => {
      if (method === 'projects.tree') {
        return { active_id: null, projects: treeWith(movedOnBackend ? 'p_dest' : 'p_src'), scoped_session_ids: ['s1'] }
      }

      if (method === 'session.workspace.move') {
        await new Promise<void>(resolve => (releaseMove = resolve))

        if (failMove) {
          throw new Error('boom')
        }

        movedOnBackend = true

        return { branch: null, cwd: '/repos/dest', git_repo_root: null }
      }

      return { active_id: null, projects: [] }
    })

    vi.mocked(activeGateway).mockReturnValue({ connectionState: 'open', request } as never)
  })

  it('files the row under the destination before the move RPC returns', async () => {
    const done = moveSessionToProject('s1', 'p_dest')

    expect($projectOwnerBySessionId.get().get('s1')).toBe('p_dest')
    expect(
      $projectTree
        .get()
        .find(n => n.id === 'p_dest')
        ?.previewSessions?.map(s => s.id)
    ).toEqual(['s1'])
    expect($projectTree.get().find(n => n.id === 'p_src')?.previewSessions).toEqual([])
    expect($sessions.get()[0].cwd).toBe('/repos/dest')

    // A tree refresh that read the backend BEFORE the move landed must not snap it back.
    await refreshProjectTree()
    expect($projectOwnerBySessionId.get().get('s1')).toBe('p_dest')

    releaseMove()
    await done
    await refreshProjectTree()
    expect($projectOwnerBySessionId.get().get('s1')).toBe('p_dest')
  })

  it('puts the row back when the move fails', async () => {
    failMove = true
    const done = moveSessionToProject('s1', 'p_dest')

    expect($projectOwnerBySessionId.get().get('s1')).toBe('p_dest')
    releaseMove()
    await expect(done).rejects.toThrow('boom')
    await refreshProjectTree()

    expect($projectOwnerBySessionId.get().get('s1')).toBe('p_src')
    expect($sessions.get()[0].cwd).toBe('/repos/src')
  })
})

describe('reassignTreeSession', () => {
  const matches = (s: SessionInfo, id: string) => s.id === id

  it('leaves a tree that already agrees untouched (control)', () => {
    const tree = treeWith('p_dest')

    expect(reassignTreeSession(tree, 's1', 'p_dest', null, matches)).toBe(tree)
  })

  it('moves ids, previews and lane rows and adjusts counts', () => {
    const tree = treeWith('p_src')
    tree[0].repos = [
      {
        groups: [{ id: 'g', label: 'main', path: '/repos/src', sessions: [row('s1', '/repos/src')] }],
        id: 'r',
        label: 'src',
        path: '/repos/src',
        sessionCount: 1
      }
    ]

    const next = reassignTreeSession(tree, 's1', 'p_dest', row('s1', '/repos/dest'), matches)

    expect(next[0].sessionIds).toEqual([])
    expect(next[0].sessionCount).toBe(0)
    expect(next[0].repos[0].groups[0].sessions).toEqual([])
    expect(next[1].sessionIds).toEqual(['s1'])
    expect(next[1].sessionCount).toBe(1)
    expect(next[1].previewSessions?.[0].cwd).toBe('/repos/dest')
  })

  it('ignores an unknown destination', () => {
    const tree = treeWith('p_src')

    expect(reassignTreeSession(tree, 's1', 'p_nope', null, matches)).toBe(tree)
  })
})

describe('createProjectAndMoveSession shows the row in the new project at once', () => {
  let releaseCreate: (() => void) | undefined
  let created: boolean
  let failCreate: boolean

  const withNew = (): SidebarProjectTree[] => [
    { ...treeWith('p_src')[0], previewSessions: [], sessionCount: 0, sessionIds: [] },
    {
      id: 'p_new',
      label: 'Fresh Idea',
      path: '/home/u/code/fresh-idea',
      previewSessions: [row('s1', '/home/u/code/fresh-idea')],
      repos: [],
      sessionCount: 1,
      sessionIds: ['s1']
    }
  ]

  beforeEach(() => {
    vi.clearAllMocks()
    setShowAllProfiles(false)
    $activeGatewayProfile.set('default')
    created = false
    failCreate = false
    releaseCreate = undefined
    $projectTree.set(treeWith('p_src'))
    setSessions(() => [row('s1', '/repos/src')])

    const request = vi.fn(async (method: string) => {
      if (method === 'projects.tree') {
        return { active_id: null, projects: created ? withNew() : treeWith('p_src'), scoped_session_ids: ['s1'] }
      }

      if (method === 'shell.exec') {
        return { code: 0, stderr: '', stdout: '' }
      }

      if (method === 'projects.create') {
        await new Promise<void>(resolve => (releaseCreate = resolve))

        if (failCreate) {
          throw new Error('create failed')
        }

        return { project: { folders: [], id: 'p_new', name: 'Fresh Idea', primary_path: '/home/u/code/fresh-idea' } }
      }

      if (method === 'session.workspace.move') {
        created = true

        return { branch: null, cwd: '/home/u/code/fresh-idea', git_repo_root: null }
      }

      return { active_id: null, projects: [] }
    })

    vi.mocked(activeGateway).mockReturnValue({ connectionState: 'open', request } as never)
  })

  it('files the row under a provisional node before projects.create returns, then the real one', async () => {
    const done = createProjectAndMoveSession('s1', 'Fresh Idea')
    const owner = $projectOwnerBySessionId.get().get('s1')

    expect(owner).toMatch(/^provisional:/)
    expect($projectTree.get().find(n => n.id === owner)?.label).toBe('Fresh Idea')

    // A refresh before the create landed keeps the provisional node and the row in it.
    await refreshProjectTree()
    expect($projectOwnerBySessionId.get().get('s1')).toBe(owner)

    await vi.waitFor(() => expect(releaseCreate).toBeTypeOf('function'))
    releaseCreate?.()
    await done
    await refreshProjectTree()

    expect($projectOwnerBySessionId.get().get('s1')).toBe('p_new')
    expect($projectTree.get().some(n => n.id.startsWith('provisional:'))).toBe(false)
  })

  it('removes the provisional node and restores the old owner when the create fails', async () => {
    failCreate = true
    const done = createProjectAndMoveSession('s1', 'Fresh Idea')

    expect($projectOwnerBySessionId.get().get('s1')).toMatch(/^provisional:/)
    await vi.waitFor(() => expect(releaseCreate).toBeTypeOf('function'))
    releaseCreate?.()
    await expect(done).rejects.toThrow('create failed')
    await refreshProjectTree()

    expect($projectOwnerBySessionId.get().get('s1')).toBe('p_src')
    expect($projectTree.get().some(n => n.id.startsWith('provisional:'))).toBe(false)
  })
})
