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
