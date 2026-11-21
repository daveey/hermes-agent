import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import type { HermesApiRequest } from '@/global'
import { $dismissedWorktreeIds } from '@/store/layout'
import { $startWorkSessionRequest, $worktreeDialog, fetchProjectSessions } from '@/store/projects'
import type * as ProjectsStore from '@/store/projects'
import { makeSessionInfo } from '@/test/session-info'

import { type EnteredMemberOptions, EnteredProjectMembers, useEnteredMember } from './entered-project-members'
import { withGatewayProjectRows } from './projects/gateway-project-rows'
import { groupProjects, presentationGroupOf } from './projects/presentation-groups'
import { projectOwnerBySessionId, type SidebarProjectTree } from './projects/workspace-groups'
import { WorktreeDialog } from './projects/worktree-dialog'

// Only the network fetch of the hydrated tree is faked. The git model
// (useRepoWorktreeMap), EnteredProjectContent, lanes and the git bridge are real.
vi.mock('@/store/projects', async importOriginal => ({
  ...(await importOriginal<typeof ProjectsStore>()),
  fetchProjectSessions: vi.fn()
}))

type ActGlobal = typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }

const STUDIO = { connectionId: 'studio', profile: 'research' }

const local = makeSessionInfo({ id: 'local', title: 'LOCAL', cwd: '/code/app', git_repo_root: '/code/app' })

const studio = makeSessionInfo({
  id: 'studio',
  title: 'STUDIO',
  connection_id: 'studio',
  profile: 'research',
  cwd: '/code/app',
  git_repo_root: '/code/app'
})

const localNode: SidebarProjectTree = {
  id: '/code/app',
  label: 'app',
  path: '/code/app',
  repos: [
    {
      id: '/code/app',
      path: '/code/app',
      label: 'app',
      groups: [
        {
          id: '/code/app::branch::main',
          label: 'main',
          path: '/code/app',
          isMain: true,
          sessions: [local]
        }
      ],
      sessionCount: 1
    }
  ],
  sessionCount: 1,
  sessionIds: [local.id],
  previewSessions: [local]
}

const owners = {
  foreignOwner: (row: { connection_id?: string | null }) => (row.connection_id === 'studio' ? 'studio' : null),
  ownerKey: (row: { connection_id?: string | null; profile?: string | null }) =>
    JSON.stringify([row.connection_id || 'local', row.profile || 'default'])
}

const tree = withGatewayProjectRows([localNode], [local, studio], owners.foreignOwner, id => id)
const studioNode = tree[1]

function installBridge() {
  const localGit = {
    branchList: vi.fn(async () => [{ isRemote: false, name: 'main' }]),
    branchSwitch: vi.fn(async () => {}),
    baseBranchList: vi.fn(async () => [{ isDefault: true, isRemote: true, name: 'origin/main' }]),
    worktreeAdd: vi.fn(async (_repo: string, branch: string) => ({ branch, path: `/code/app/.local/${branch}` })),
    worktreeList: vi.fn(async () => [
      { branch: 'local-wt', detached: false, isMain: false, locked: false, path: '/code/app/.worktrees/local-wt' }
    ])
  }

  const api = vi.fn(async (request: HermesApiRequest) => {
    if (request.path.startsWith('/api/git/base-branches')) {
      return { branches: [{ isDefault: true, isRemote: true, name: 'origin/main' }] }
    }

    if (request.path === '/api/git/worktree/add') {
      const body = request.body as { branch: string }

      return { branch: body.branch, path: `/code/app/.worktrees/${body.branch}` }
    }

    if (request.path.startsWith('/api/git/branches')) {
      return { branches: [{ isRemote: false, name: 'main' }] }
    }

    if (request.path === '/api/git/branch/switch') {
      return {}
    }

    if (request.path.startsWith('/api/git/worktrees')) {
      return {
        worktrees: [
          { branch: 'studio-wt', detached: false, isMain: false, locked: false, path: '/code/app/.worktrees/studio-wt' }
        ]
      }
    }

    throw new Error(`unexpected ${request.path}`)
  })

  ;(window as { hermesDesktop?: unknown }).hermesDesktop = { api, git: localGit }

  return { api, localGit }
}

const options: EnteredMemberOptions = {
  gatewayReady: true,
  projectTree: tree,
  scope: 'local:default',
  grouped: true,
  orderRepos: repos => repos,
  isHiddenFromProjects: () => false,
  agentSessions: [local, studio],
  owners,
  removedSessionIds: new Set(),
  projectOwners: projectOwnerBySessionId(tree),
  projects: [],
  knownProjects: { ids: new Set(), roots: [] },
  ownerKey: owners.ownerKey,
  showAllProfiles: false
}

function mount(onNewSession = vi.fn()) {
  function Body() {
    const primary = useEnteredMember(tree[0], options)

    return (
      <EnteredProjectMembers
        members={tree}
        onNewSession={onNewSession}
        options={options}
        primary={primary}
        renderRows={rows => rows.map(row => <span key={row.id}>{row.title}</span>)}
      />
    )
  }

  render(
    <SidebarProvider>
      <Body />
      <WorktreeDialog />
    </SidebarProvider>
  )

  return { onNewSession }
}

const section = (id: string) => {
  const el = Array.from(document.querySelectorAll('[data-entered-project-member]')).find(
    node => node.getAttribute('data-entered-project-member') === id
  )

  if (!el) {
    throw new Error(`no member section ${id}`)
  }

  return within(el as HTMLElement)
}

beforeEach(() => {
  ;(globalThis as ActGlobal).IS_REACT_ACT_ENVIRONMENT = false
  vi.mocked(fetchProjectSessions).mockImplementation(async id => tree.find(node => node.id === id) ?? null)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  $worktreeDialog.set(null)
  $dismissedWorktreeIds.set([])
  $startWorkSessionRequest.set(null)
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
  ;(globalThis as ActGlobal).IS_REACT_ACT_ENVIRONMENT = true
})

describe('grouped entered project: local + studio at the same path, real git model', () => {
  it('groups both nodes, renders two member sections with one row each and per-owner worktree lanes', async () => {
    const { api, localGit } = installBridge()

    expect(tree.map(node => node.id)).toEqual(['/code/app', studioNode.id])
    expect(presentationGroupOf(groupProjects(tree), studioNode.id)?.primary).toBe(localNode)

    mount()

    await waitFor(() => expect(screen.getByTitle(/studio-wt/)).toBeTruthy())
    await waitFor(() => expect(screen.getByTitle(/local-wt/)).toBeTruthy())

    expect(document.querySelectorAll('[data-entered-project-member]')).toHaveLength(2)

    const l = section(localNode.id)
    const s = section(studioNode.id)

    expect(l.getAllByText('LOCAL')).toHaveLength(1)
    expect(l.queryByText('STUDIO')).toBeNull()
    expect(s.getAllByText('STUDIO')).toHaveLength(1)
    expect(s.queryByText('LOCAL')).toBeNull()

    // Worktree discovery is per owner: studio's list came over the bridge, local's from this window's git.
    expect(l.getByTitle(/local-wt/)).toBeTruthy()
    expect(l.queryByTitle(/studio-wt/)).toBeNull()
    expect(s.getByTitle(/studio-wt/)).toBeTruthy()
    expect(s.queryByTitle(/local-wt/)).toBeNull()

    const wtCalls = api.mock.calls.map(([r]) => r).filter(r => r.path.startsWith('/api/git/worktrees'))

    expect(wtCalls).toHaveLength(1)
    expect(wtCalls[0]).toMatchObject({ ...STUDIO, path: '/api/git/worktrees?path=%2Fcode%2Fapp' })
    expect(localGit.worktreeList).toHaveBeenCalledTimes(1)
    expect(localGit.worktreeList).toHaveBeenCalledWith('/code/app')
  })

  it("studio lane 'new session' switches branch on studio and starts there; local lane stays local", async () => {
    const { api, localGit } = installBridge()
    const { onNewSession } = mount()

    await waitFor(() => expect(screen.getByTitle(/studio-wt/)).toBeTruthy())

    // Studio's main lane first.
    fireEvent.click(section(studioNode.id).getAllByRole('button', { name: /new session/i })[0])

    await waitFor(() => expect(onNewSession).toHaveBeenCalledWith('/code/app', STUDIO))
    expect(api.mock.calls.map(([r]) => r).filter(r => r.path === '/api/git/branch/switch')).toEqual([
      expect.objectContaining({ ...STUDIO, body: { branch: 'main', path: '/code/app' } })
    ])
    expect(localGit.branchSwitch).not.toHaveBeenCalled()

    onNewSession.mockClear()
    api.mockClear()

    // Local's main lane: this window's git, no owner route.
    fireEvent.click(section(localNode.id).getAllByRole('button', { name: /new session/i })[0])

    await waitFor(() => expect(onNewSession).toHaveBeenCalledTimes(1))
    expect(onNewSession.mock.calls[0][0]).toBe('/code/app')
    expect(onNewSession.mock.calls[0][1]).toBeUndefined()
    expect(localGit.branchSwitch).toHaveBeenCalledWith('/code/app', 'main')
    expect(api.mock.calls.map(([r]) => r).filter(r => r.path.startsWith('/api/git/branch'))).toEqual([])
  })

  it('studio New worktree dialog creates on the studio gateway and starts its session there', async () => {
    const { api, localGit } = installBridge()

    mount()
    fireEvent.click(section(studioNode.id).getByRole('button', { name: 'New worktree' }))
    fireEvent.change(await screen.findByPlaceholderText('e.g. my-feature'), { target: { value: 'my-work' } })
    await screen.findByText('origin/main')
    fireEvent.click(screen.getAllByRole('button', { name: 'New worktree' }).at(-1)!)

    await waitFor(() => expect($startWorkSessionRequest.get()).not.toBeNull())

    const add = api.mock.calls.map(([r]) => r).find(r => r.path === '/api/git/worktree/add')

    expect(add).toMatchObject({ ...STUDIO, method: 'POST', body: { branch: 'my-work', path: '/code/app' } })
    expect(localGit.worktreeAdd).not.toHaveBeenCalled()
    expect($startWorkSessionRequest.get()).toMatchObject({ owner: STUDIO, path: '/code/app/.worktrees/my-work' })
  })

  it('local New worktree stays on this window git (contrast)', async () => {
    const { api, localGit } = installBridge()

    mount()
    fireEvent.click(section(localNode.id).getByRole('button', { name: 'New worktree' }))
    fireEvent.change(await screen.findByPlaceholderText('e.g. my-feature'), { target: { value: 'my-work' } })
    await screen.findByText('origin/main')
    fireEvent.click(screen.getAllByRole('button', { name: 'New worktree' }).at(-1)!)

    await waitFor(() => expect(localGit.worktreeAdd).toHaveBeenCalled())
    expect(api.mock.calls.map(([r]) => r).some(r => r.path === '/api/git/worktree/add')).toBe(false)
  })

  it('a failing studio member load does not blank or error the local member', async () => {
    installBridge()
    vi.mocked(fetchProjectSessions).mockImplementation(async id => {
      if (id === studioNode.id) {
        throw new Error('studio down')
      }

      return tree.find(node => node.id === id) ?? null
    })

    mount()

    await waitFor(() => expect(fetchProjectSessions).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(document.querySelectorAll('[data-entered-project-member]')).toHaveLength(2))

    const l = section(localNode.id)

    expect(l.getAllByText('LOCAL')).toHaveLength(1)
    expect(l.queryByRole('button', { name: /retry/i })).toBeNull()
    await waitFor(() => expect(section(studioNode.id).getByRole('button', { name: /retry/i })).toBeTruthy())
  })

  it('a failing local (primary) member load does not blank or error the studio member', async () => {
    installBridge()
    vi.mocked(fetchProjectSessions).mockImplementation(async id => {
      if (id === localNode.id) {
        throw new Error('local down')
      }

      return tree.find(node => node.id === id) ?? null
    })

    mount()

    await waitFor(() => expect(fetchProjectSessions).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(section(studioNode.id).getAllByText('STUDIO')).toHaveLength(1))
    expect(section(studioNode.id).queryByRole('button', { name: /retry/i })).toBeNull()
    expect(section(localNode.id).queryAllByText('STUDIO')).toHaveLength(0)
    await waitFor(() => expect(section(localNode.id).getByRole('button', { name: /retry/i })).toBeTruthy())
  })

  it('a pending foreign member preserves the ready primary and its own cached rows', async () => {
    installBridge()
    vi.mocked(fetchProjectSessions).mockImplementation(id =>
      id === studioNode.id ? new Promise(() => {}) : Promise.resolve(localNode)
    )
    mount()
    await waitFor(() => expect(fetchProjectSessions).toHaveBeenCalledTimes(2))
    expect(section(localNode.id).getAllByText('LOCAL')).toHaveLength(1)
    expect(section(studioNode.id).getAllByText('STUDIO')).toHaveLength(1)
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
  })
})
