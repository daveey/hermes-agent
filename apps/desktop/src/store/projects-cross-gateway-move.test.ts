import { atom } from 'nanostores'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { activeGateway, activeGatewayConnectionId, isActivePrimary, requestGatewayForAgent } from '@/store/gateway'
import { $activeGatewayProfile, setShowAllProfiles } from '@/store/profile'
import { $sessions } from '@/store/session'

import { $connectionsRegistry } from './connection-registry-state'
import {
  $projectTree,
  createProjectAndMoveSession,
  homeRelativePath,
  moveSessionToProject,
  moveToProjectTargets,
  newProjectFolder
} from './projects'

vi.mock('@/store/gateway', () => ({
  $gateway: atom(null),
  activeGateway: vi.fn(),
  activeGatewayConnectionId: vi.fn(),
  isActivePrimary: vi.fn(),
  ensureActiveGatewayOpen: vi.fn(),
  requestGatewayForAgent: vi.fn()
}))
vi.mock('@/i18n', () => ({ translateNow: (key: string) => key }))
vi.mock('@/hermes', () => ({
  getHermesConfig: vi.fn(),
  getProfiles: vi.fn(),
  hermesApi: vi.fn(),
  setApiRequestProfile: vi.fn(),
  STARTUP_REQUEST_TIMEOUT_MS: 1000
}))

const LOCAL_PATH = '/Users/daveey/code/babybear'

const tree: SidebarProjectTree[] = [
  { id: 'p_local', label: 'babybear', path: LOCAL_PATH, repos: [], sessionCount: 1 },
  { id: 'p_other', label: 'other', path: '/Users/daveey/code/other', repos: [], sessionCount: 0 }
]

type Handler = (method: string, params: Record<string, unknown>) => unknown

let localCalls: [string, Record<string, unknown>][] = []
let remoteCalls: [string, string, Record<string, unknown>][] = []

function setup(local: Handler, remote: Handler) {
  localCalls = []
  remoteCalls = []
  vi.mocked(activeGateway).mockReturnValue({
    connectionState: 'open',
    request: vi.fn(async (method: string, params: Record<string, unknown>) => {
      localCalls.push([method, params])

      return local(method, params)
    })
  } as never)
  vi.mocked(requestGatewayForAgent).mockImplementation((async (
    conn: string,
    _profile: string,
    method: string,
    params: Record<string, unknown>
  ) => {
    remoteCalls.push([conn, method, params])

    return remote(method, params)
  }) as never)
}

const localDefault: Handler = method =>
  method === 'projects.tree'
    ? { active_id: null, projects: tree, scoped_session_ids: [] }
    : method === 'projects.list'
      ? { active_id: null, projects: [] }
      : method === 'session.workspace.move'
        ? { branch: 'main', cwd: LOCAL_PATH, git_repo_root: LOCAL_PATH }
        : {}

beforeEach(() => {
  vi.clearAllMocks()
  setShowAllProfiles(false)
  $activeGatewayProfile.set('default')
  vi.mocked(activeGatewayConnectionId).mockReturnValue('local')
  vi.mocked(isActivePrimary).mockReturnValue(true)
  $connectionsRegistry.set({
    connections: [
      { id: 'local', kind: 'local', label: 'This device' },
      { id: 'mac-mini', kind: 'ssh', label: 'mac-mini' }
    ],
    primary: 'local'
  } as never)
  $projectTree.set(tree)
  $sessions.set([
    { id: 'remote-s', profile: 'default', connection_id: 'mac-mini', cwd: '/Users/daveey' } as never,
    { id: 'local-s', profile: 'default', cwd: '/Users/daveey/code/other' } as never
  ])
})

describe('moveSessionToProject across gateways', () => {
  it('control: a local session moves on the active gateway to the local project root', async () => {
    setup(localDefault, () => {
      throw new Error('no remote call expected')
    })
    await moveSessionToProject('local-s', 'p_local', 'default')
    expect(localCalls.find(([m]) => m === 'session.workspace.move')?.[1]).toMatchObject({
      cwd: LOCAL_PATH,
      session_key: 'local-s'
    })
    expect(remoteCalls).toEqual([])
  })

  it('moves a remote session on ITS gateway, creating the project there when missing', async () => {
    setup(localDefault, method =>
      method === 'projects.list'
        ? { active_id: null, projects: [] }
        : method === 'shell.exec'
          ? { code: 0, stderr: '', stdout: '' }
          : method === 'projects.create'
            ? { project: { id: 'p_new', name: 'babybear', primary_path: '/Users/daveey/code/babybear' } }
            : method === 'session.workspace.move'
              ? { branch: 'main', cwd: '/Users/daveey/code/babybear', git_repo_root: null }
              : {}
    )

    await moveSessionToProject('remote-s', 'p_local', 'default')

    expect(localCalls.some(([m]) => m === 'session.workspace.move')).toBe(false)
    expect(remoteCalls.map(([conn, m]) => `${conn}:${m}`)).toEqual([
      'mac-mini:projects.list',
      'mac-mini:shell.exec',
      'mac-mini:projects.create',
      'mac-mini:session.workspace.move'
    ])
    expect(remoteCalls[2][2]).toMatchObject({ name: 'babybear', primary_path: '~/code/babybear' })
    expect(remoteCalls[3][2]).toMatchObject({ cwd: '/Users/daveey/code/babybear', session_key: 'remote-s' })
  })

  it("reuses the remote gateway's existing same-named project", async () => {
    setup(localDefault, method =>
      method === 'projects.list'
        ? {
            active_id: null,
            projects: [{ id: 'p_r', name: 'BabyBear', slug: 'babybear', primary_path: '/srv/bb', archived: false }]
          }
        : method === 'session.workspace.move'
          ? { branch: null, cwd: '/srv/bb', git_repo_root: null }
          : {}
    )

    await moveSessionToProject('remote-s', 'p_local')

    expect(remoteCalls.map(([, m]) => m)).toEqual(['projects.list', 'session.workspace.move'])
    expect(remoteCalls[1][2]).toMatchObject({ cwd: '/srv/bb' })
  })

  it('creates an empty folder on the remote (no clone) before creating the project', async () => {
    setup(localDefault, method =>
      method === 'projects.list'
        ? { active_id: null, projects: [] }
        : method === 'shell.exec'
          ? { code: 0, stderr: '', stdout: '' }
          : method === 'projects.create'
            ? { project: { primary_path: '/home/me/code/babybear' } }
            : method === 'session.workspace.move'
              ? { branch: null, cwd: '/home/me/code/babybear', git_repo_root: null }
              : {}
    )

    await moveSessionToProject('remote-s', 'p_local')

    const shells = remoteCalls.filter(([, m]) => m === 'shell.exec').map(([, , p]) => String(p.command))
    expect(shells).toEqual([`mkdir -p "$HOME"/'code/babybear'`])
    expect(localCalls.some(([m]) => m === 'shell.exec')).toBe(false)
    expect(remoteCalls.at(-1)?.[2]).toMatchObject({ cwd: '/home/me/code/babybear' })
  })

  it("reuses the remote's differently-named project that already owns the folder", async () => {
    // The Mac calls ~/code/babybear "babybear"; the remote registered the same
    // folder under another name, so projects.create refuses the duplicate.
    setup(localDefault, (method, params) => {
      if (method === 'projects.list') {
        return {
          active_id: null,
          projects: [{ id: 'p_ad616738', name: 'focus', slug: 'focus', primary_path: '/home/me/code/babybear' }]
        }
      }

      if (method === 'shell.exec') {
        return { code: 0, stderr: '', stdout: '' }
      }

      if (method === 'projects.create') {
        throw new Error(
          "folder already belongs to project 'focus' (p_ad616738); switch to it instead of creating a duplicate"
        )
      }

      if (method === 'projects.get') {
        expect(params).toMatchObject({ id: 'p_ad616738' })

        return { project: { id: 'p_ad616738', name: 'focus', primary_path: '/home/me/code/babybear' } }
      }

      return method === 'session.workspace.move'
        ? { branch: null, cwd: '/home/me/code/babybear', git_repo_root: null }
        : {}
    })

    await moveSessionToProject('remote-s', 'p_local')

    expect(remoteCalls.map(([, m]) => m)).toEqual([
      'projects.list',
      'shell.exec',
      'projects.create',
      'projects.get',
      'session.workspace.move'
    ])
    expect(remoteCalls.at(-1)?.[2]).toMatchObject({ cwd: '/home/me/code/babybear', session_key: 'remote-s' })
  })

  it('still surfaces any other projects.create error', async () => {
    setup(localDefault, method => {
      if (method === 'projects.list') {
        return { active_id: null, projects: [] }
      }

      if (method === 'shell.exec') {
        return { code: 0, stderr: '', stdout: '' }
      }

      if (method === 'projects.create') {
        throw new Error('project name must not be empty')
      }

      return {}
    })

    await expect(moveSessionToProject('remote-s', 'p_local')).rejects.toThrow('project name must not be empty')
    expect(remoteCalls.some(([, m]) => m === 'session.workspace.move')).toBe(false)
  })

  it('surfaces a failed mkdir instead of moving', async () => {
    setup(localDefault, method =>
      method === 'projects.list'
        ? { active_id: null, projects: [] }
        : method === 'shell.exec'
          ? { code: 1, stderr: 'Permission denied', stdout: '' }
          : {}
    )

    await expect(moveSessionToProject('remote-s', 'p_local')).rejects.toThrow(/Permission denied/)
    expect(remoteCalls.some(([, m]) => m === 'session.workspace.move')).toBe(false)
  })
})

describe('moveSessionToProject into a lane whose folder this gateway lacks', () => {
  // Another profile on the active gateway (a Docker one) owns the lane, so its
  // root is a container path the host refuses.
  const DOCKER_PATH = '/opt/data/code/fabric-play'

  const refusing =
    (extra: Handler): Handler =>
    (method, params) => {
      if (method === 'session.workspace.move' && params.cwd === DOCKER_PATH) {
        throw new Error(`working directory does not exist: ${DOCKER_PATH}`)
      }

      if (method === 'session.workspace.move') {
        return { branch: null, cwd: params.cwd, git_repo_root: null }
      }

      return extra(method, params)
    }

  beforeEach(() => {
    $projectTree.set([{ id: 'p_docker', label: 'fabric', path: DOCKER_PATH, repos: [], sessionCount: 1 }])
  })

  it("creates the project in the session's home instead of failing", async () => {
    setup(
      refusing(method =>
        method === 'projects.list'
          ? { active_id: null, projects: [] }
          : method === 'shell.exec'
            ? { code: 0, stderr: '', stdout: '' }
            : method === 'projects.create'
              ? { project: { id: 'p_new', name: 'fabric', primary_path: '/home/ubuntu/code/fabric-play' } }
              : {}
      ),
      () => {
        throw new Error('no remote call expected')
      }
    )

    await moveSessionToProject('local-s', 'p_docker', 'default')

    // Background refreshes (projects.tree, a post-create projects.list) interleave; only the move's own calls count.
    const firstList = localCalls.findIndex(([m]) => m === 'projects.list')
    const steps = localCalls.filter(([m], i) => m !== 'projects.tree' && (m !== 'projects.list' || i === firstList))

    expect(steps.map(([m]) => m)).toEqual([
      'session.workspace.move',
      'projects.list',
      'shell.exec',
      'projects.create',
      'session.workspace.move'
    ])
    expect(steps[2][1]).toMatchObject({ command: `mkdir -p "$HOME"/'code/fabric-play'` })
    expect(steps[3][1]).toMatchObject({ name: 'fabric', primary_path: '~/code/fabric-play' })
    expect(steps[4][1]).toMatchObject({ cwd: '/home/ubuntu/code/fabric-play', session_key: 'local-s' })
  })

  it("makes the folder of the session profile's own project at that path", async () => {
    setup(
      refusing(method =>
        method === 'projects.list'
          ? { active_id: null, projects: [{ id: 'p_docker', name: 'fabric', primary_path: DOCKER_PATH }] }
          : method === 'shell.exec'
            ? { code: 1, stderr: 'mkdir: /opt: Permission denied', stdout: '' }
            : {}
      ),
      () => {
        throw new Error('no remote call expected')
      }
    )

    await expect(moveSessionToProject('local-s', 'p_docker', 'default')).rejects.toThrow(/Permission denied/)
    expect(localCalls.map(([m]) => m).filter(m => m !== 'projects.tree')).toEqual([
      'session.workspace.move',
      'projects.list',
      'shell.exec'
    ])
    expect(localCalls.some(([m]) => m === 'projects.create')).toBe(false)
  })

  it('still surfaces any other move error', async () => {
    setup(
      method => {
        if (method === 'session.workspace.move') {
          throw new Error('session busy')
        }

        return {}
      },
      () => ({})
    )

    await expect(moveSessionToProject('local-s', 'p_docker', 'default')).rejects.toThrow(/session busy/)
    expect(localCalls.map(([m]) => m).filter(m => m !== 'projects.tree')).toEqual(['session.workspace.move'])
  })
})

describe('createProjectAndMoveSession', () => {
  it('creates an empty ~/code/<slug> folder + project, then moves the session', async () => {
    setup(
      method =>
        method === 'projects.list'
          ? { active_id: null, projects: [] }
          : method === 'shell.exec'
            ? { code: 0, stderr: '', stdout: '' }
            : method === 'projects.create'
              ? { project: { id: 'p_n', name: 'My Thing', primary_path: '/Users/daveey/code/my-thing' } }
              : method === 'session.workspace.move'
                ? { branch: null, cwd: '/Users/daveey/code/my-thing', git_repo_root: null }
                : {},
      () => {
        throw new Error('no remote call expected')
      }
    )

    await createProjectAndMoveSession('local-s', ' My Thing ', 'default')

    // (refreshProjects/refreshProjectTree fire background list/tree reads too.)
    const methods = localCalls.map(([m]) => m).filter(m => m !== 'projects.tree' && m !== 'projects.list')
    expect(localCalls[0][0]).toBe('projects.list')
    expect(methods).toEqual(['shell.exec', 'projects.create', 'session.workspace.move'])
    expect(localCalls.find(([m]) => m === 'shell.exec')?.[1]).toEqual({ command: `mkdir -p "$HOME"/'code/my-thing'` })
    expect(localCalls.find(([m]) => m === 'projects.create')?.[1]).toMatchObject({
      name: 'My Thing',
      primary_path: '~/code/my-thing'
    })
    expect(localCalls.find(([m]) => m === 'session.workspace.move')?.[1]).toMatchObject({
      cwd: '/Users/daveey/code/my-thing',
      session_key: 'local-s'
    })
  })

  it('reuses a same-named project on the gateway instead of creating one', async () => {
    setup(
      method =>
        method === 'projects.list'
          ? { active_id: null, projects: [{ id: 'p', name: 'Thing', slug: 'thing', primary_path: '/x/thing' }] }
          : method === 'session.workspace.move'
            ? { branch: null, cwd: '/x/thing', git_repo_root: null }
            : {},
      () => ({})
    )

    await createProjectAndMoveSession('local-s', 'thing', 'default')
    expect(localCalls.some(([m]) => m === 'shell.exec' || m === 'projects.create')).toBe(false)
  })

  it('slugs names into ~/code', () => {
    expect(newProjectFolder('My Cool Project!')).toBe('~/code/my-cool-project')
    expect(newProjectFolder('../x')).toBe('~/code/x')
    expect(newProjectFolder('  ')).toBe('~/code/project')
  })
})

describe('cross-gateway move helpers', () => {
  it('maps home paths across hosts', () => {
    expect(homeRelativePath('/Users/daveey/code/babybear')).toBe('~/code/babybear')
    expect(homeRelativePath('/home/ubuntu/code/x')).toBe('~/code/x')
    expect(homeRelativePath('/opt/data/code/fabric-play')).toBe('~/code/fabric-play')
    expect(homeRelativePath('/opt/database')).toBe('/opt/database')
    expect(homeRelativePath('/srv/repo')).toBe('/srv/repo')
  })

  it('offers remote-only project lanes as move targets', () => {
    const remoteLane = {
      id: 'gw:mac-mini:p_x',
      label: 'mini-only',
      path: null,
      repos: [],
      sessionCount: 1
    } as SidebarProjectTree

    const ids = moveToProjectTargets([...tree, remoteLane], $sessions.get(), 'local-s').map(n => n.id)
    expect(ids).toContain('gw:mac-mini:p_x')
  })
})
