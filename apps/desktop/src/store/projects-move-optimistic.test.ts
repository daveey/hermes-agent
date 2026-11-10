import { atom } from 'nanostores'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { activeGateway } from '@/store/gateway'
import { $activeGatewayProfile, setShowAllProfiles } from '@/store/profile'
import { $sessions, setSessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { $projectOwnerBySessionId, $projectTree, moveSessionToProject, refreshProjectTree } from './projects'
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
    expect($projectTree.get().find(n => n.id === 'p_dest')?.previewSessions?.map(s => s.id)).toEqual(['s1'])
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
