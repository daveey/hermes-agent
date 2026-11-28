import { atom } from 'nanostores'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { hermesApi } from '@/hermes'
import { activeGateway, activeGatewayConnectionId, isActivePrimary, requestGatewayForAgent } from '@/store/gateway'
import { $activeGatewayProfile, setShowAllProfiles } from '@/store/profile'
import { $sessions } from '@/store/session'

import { $connectionsRegistry } from './connection-registry-state'
import { hubGatewayForConnection, hubGatewayProjects, type HubProjectNode, hubRequest } from './hub-sync'
import { $projectTree, refreshProjectTree, updateProject } from './projects'

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
vi.mock('./hub-sync', () => ({
  hubGatewayForConnection: vi.fn(),
  hubGatewayProjects: vi.fn(),
  hubRequest: vi.fn(),
  learnHubGatewayName: vi.fn()
}))

// [daveey] Project groups on the hub (phase 6b): a remote this Desktop can't reach is drawn from the hub replica,
// and its declared projects rename / theme as hub shared fields, owner awake or not.

const localTree: SidebarProjectTree[] = [
  { id: 'p_local', label: 'local-only', path: '/Users/x/local', repos: [], sessionCount: 0 }
]

const replicaNode = (over: Partial<HubProjectNode> = {}): HubProjectNode => ({
  id: 'p_garden',
  label: 'Garden',
  path: '/srv/garden',
  color: null,
  icon: null,
  isAuto: false,
  isNoProject: false,
  sessionCount: 1,
  lastActive: 5,
  repos: [],
  previewSessions: [
    {
      gateway: 'mini',
      id: 's_1',
      profile: 'default',
      title: 'mini chat',
      message_count: 3,
      activity: 5,
      synced_at: 1,
      stale: false
    }
  ] as never,
  sessionIds: ['s_1'],
  members: [
    { gateway: 'mini', profile: 'default', id: 'p_garden', key: 'default/p_garden', kind: 'explicit' },
    { gateway: 'mini', profile: 'bb', id: 'p_garden2', key: 'bb/p_garden2', kind: 'explicit' },
    { gateway: 'mini', profile: 'ops', id: '/srv/garden', key: 'ops/auto:1', kind: 'auto' }
  ],
  ...over
})

const flush = async () => {
  for (let i = 0; i < 20; i += 1) {
    await Promise.resolve()
  }
}

// remoteTrees is module state kept per connection id, so each test uses its own connection (as the other
// remote-tree tests do).
let conn = 'mini-0'
const remoteNode = () => $projectTree.get().find(node => node.id === `gw:${conn}:p_garden`)

beforeEach(ctx => {
  vi.clearAllMocks()
  conn = `mini-${ctx.task.id}`
  setShowAllProfiles(false)
  $activeGatewayProfile.set('default')
  $sessions.set([])
  $projectTree.set([])
  $connectionsRegistry.set({
    connections: [
      { id: 'local', kind: 'local', label: 'This device' },
      { id: conn, kind: 'remote', label: 'mini' }
    ],
    primary: 'local'
  } as never)
  vi.mocked(activeGatewayConnectionId).mockReturnValue('local')
  vi.mocked(isActivePrimary).mockReturnValue(true)
  vi.mocked(activeGateway).mockReturnValue({
    connectionState: 'open',
    request: vi.fn(async (method: string) =>
      method === 'projects.tree' ? { active_id: null, projects: localTree, scoped_session_ids: [] } : {}
    )
  } as never)
  vi.mocked(hubGatewayForConnection).mockImplementation(id => (id === conn ? 'mini' : undefined))
  vi.mocked(hubRequest).mockResolvedValue({ id: 'hr-1', queued_locally: false, state: 'applied' })
})

describe('remote project groups from the hub replica', () => {
  it('does not wake a local backend for background trees, but reads one already running', async () => {
    vi.mocked(activeGatewayConnectionId).mockReturnValue(conn)
    $connectionsRegistry.set({
      connections: [
        { id: 'local', kind: 'local', label: 'This device' },
        { id: conn, kind: 'remote', label: 'remote' }
      ],
      primary: conn
    } as never)
    vi.mocked(hubGatewayProjects).mockResolvedValue(null)
    vi.mocked(hermesApi).mockRejectedValue(new Error('passive backend unavailable'))
    await refreshProjectTree()
    await flush()
    expect(hermesApi).toHaveBeenCalledWith(expect.objectContaining({ connectionId: 'local', passive: true }))
    expect(requestGatewayForAgent).not.toHaveBeenCalled()
    const clock = vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 120_000)

    try {
      vi.mocked(hermesApi).mockResolvedValue({
        projects: [{ ...localTree[0], id: 'p_warm', label: 'warm-only' }],
        scoped_session_ids: []
      })
      await refreshProjectTree()
      await flush()
      expect($projectTree.get().some(node => node.id === 'gw:local:p_warm')).toBe(true)
      expect(requestGatewayForAgent).not.toHaveBeenCalled()
    } finally {
      clock.mockRestore()
    }
  })

  it("draws an unreachable gateway's groups from the replica, renameable, rows owned by that connection", async () => {
    vi.mocked(hermesApi).mockRejectedValue(new Error('ssh: connect timed out'))
    vi.mocked(requestGatewayForAgent).mockRejectedValue(new Error('ssh: connect timed out'))
    vi.mocked(hubGatewayProjects).mockResolvedValue([
      replicaNode(),
      replicaNode({ id: '/srv/auto', label: 'auto', path: '/srv/auto', isAuto: true, members: [], previewSessions: [] })
    ])

    await refreshProjectTree()
    await flush()

    expect(hubGatewayProjects).toHaveBeenCalledWith('mini', expect.any(Number))
    const node = remoteNode()
    expect(node).toMatchObject({ label: 'Garden', remoteProject: { connectionId: conn, id: 'p_garden' } })
    expect(node?.previewSessions?.[0]).toMatchObject({ id: 's_1', connection_id: conn, title: 'mini chat' })
    expect(node).not.toHaveProperty('members')
    expect($projectTree.get().find(n => n.id === `gw:${conn}:/srv/auto`)?.remoteProject).toBeUndefined()
  })

  it('paints from the replica before the first live answer, then the live tree wins', async () => {
    let answer: (v: unknown) => void = () => {}
    vi.mocked(hermesApi).mockReturnValue(new Promise(resolve => (answer = resolve)) as never)
    vi.mocked(hubGatewayProjects).mockResolvedValue([replicaNode({ label: 'Garden (replica)' })])

    await refreshProjectTree()
    await flush()
    expect(remoteNode()?.label).toBe('Garden (replica)')

    answer({
      active_id: null,
      projects: [{ ...replicaNode(), members: undefined, label: 'Garden (live)' }],
      scoped_session_ids: []
    })
    await flush()
    expect(remoteNode()?.label).toBe('Garden (live)')
  })

  // [daveey] Replica records carry no task annotations. Swapping a gateway's rows for them must not wipe the
  // kanban badge it last reported (audit: 17 rows of This device showed no open/blocked counts for 16 min after a
  // restart while the hub was the active gateway).
  const liveRow = (over: Record<string, unknown> = {}) => ({
    id: 's_1',
    profile: 'default',
    title: 'mini chat',
    message_count: 3,
    started_at: 1,
    last_active: 5,
    kanban_active: 2,
    kanban_blocked: 1,
    kanban_parked: 2,
    kanban_observed_at: 1000,
    ...over
  })

  const remoteRow = () => remoteNode()?.previewSessions?.find(row => row.id === 's_1')

  it('keeps the last live kanban counts when a failed refresh falls back to the replica', async () => {
    vi.mocked(hubGatewayProjects).mockResolvedValue([replicaNode()])
    vi.mocked(hermesApi).mockResolvedValue({
      active_id: null,
      projects: [{ ...replicaNode(), members: undefined, previewSessions: [liveRow()] }],
      scoped_session_ids: []
    })
    await refreshProjectTree()
    await flush()
    expect(remoteRow()).toMatchObject({ kanban_active: 2, kanban_blocked: 1 })

    const clock = vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 60_000)

    try {
      vi.mocked(hermesApi).mockRejectedValue(new Error('ssh: connect timed out'))
      vi.mocked(requestGatewayForAgent).mockRejectedValue(new Error('ssh: connect timed out'))
      await refreshProjectTree()
      await flush()
    } finally {
      clock.mockRestore()
    }

    expect(vi.mocked(hubGatewayProjects).mock.calls.length).toBeGreaterThanOrEqual(2)
    expect(remoteRow()).toMatchObject({ kanban_active: 2, kanban_blocked: 1, kanban_observed_at: 1000 })
  })

  it('keeps the boot snapshot kanban counts while the first live answer is pending', async () => {
    $projectTree.set([
      {
        ...(replicaNode() as never as SidebarProjectTree),
        id: `gw:${conn}:p_garden`,
        previewSessions: [liveRow({ connection_id: conn, kanban_active: 3 })] as never
      }
    ])
    vi.mocked(hermesApi).mockReturnValue(new Promise(() => {}) as never)
    vi.mocked(hubGatewayProjects).mockResolvedValue([replicaNode()])

    await refreshProjectTree()
    await flush()

    expect(remoteRow()).toMatchObject({ connection_id: conn, kanban_active: 3, kanban_blocked: 1 })
  })

  it('never borrows task evidence from another gateway or invents it for an unseen row', async () => {
    $projectTree.set([
      {
        ...(replicaNode() as never as SidebarProjectTree),
        id: 'gw:other:p_garden',
        previewSessions: [liveRow({ connection_id: 'other', kanban_active: 9 })] as never
      }
    ])
    vi.mocked(hermesApi).mockReturnValue(new Promise(() => {}) as never)
    vi.mocked(hubGatewayProjects).mockResolvedValue([replicaNode()])

    await refreshProjectTree()
    await flush()

    expect(remoteRow()).toBeDefined()
    expect(remoteRow()).not.toHaveProperty('kanban_active')
    expect(remoteRow()).not.toHaveProperty('kanban_observed_at')
  })

  it('renames a remote declared project as one shared set per declared member, and keeps the new name', async () => {
    vi.mocked(hermesApi).mockRejectedValue(new Error('asleep'))
    vi.mocked(requestGatewayForAgent).mockRejectedValue(new Error('asleep'))
    vi.mocked(hubGatewayProjects).mockResolvedValue([replicaNode()])
    await refreshProjectTree()
    await flush()

    await updateProject(`gw:${conn}:p_garden`, { name: 'Garden RL' })

    expect(remoteNode()?.label).toBe('Garden RL')
    expect(vi.mocked(hubRequest).mock.calls.map(([body]) => body)).toEqual([
      { action: 'rename', args: { name: 'Garden RL' }, coll: 'projects', gateway: 'mini', key: 'default/p_garden' },
      { action: 'rename', args: { name: 'Garden RL' }, coll: 'projects', gateway: 'mini', key: 'bb/p_garden2' }
    ])
    // A repaint before the next fetch (sessions.changed, focus) keeps the new name.
    await refreshProjectTree()
    await flush()
    expect(remoteNode()?.label).toBe('Garden RL')
  })

  it('sends a cleared icon or color as "" and rolls back when the hub refuses', async () => {
    vi.mocked(hermesApi).mockRejectedValue(new Error('asleep'))
    vi.mocked(requestGatewayForAgent).mockRejectedValue(new Error('asleep'))
    vi.mocked(hubGatewayProjects).mockResolvedValue([replicaNode({ color: '#ff8800' })])
    await refreshProjectTree()
    await flush()

    await updateProject(`gw:${conn}:p_garden`, { color: null })
    expect(vi.mocked(hubRequest).mock.calls[0][0]).toMatchObject({ action: 'color', args: { color: '' } })

    vi.mocked(hubRequest).mockRejectedValueOnce(new Error('409 projects on other gateways change only in primary'))
    await expect(updateProject(`gw:${conn}:p_garden`, { icon: 'rocket' })).rejects.toThrow('409')
    expect(remoteNode()?.icon ?? null).toBeNull()
  })
})
