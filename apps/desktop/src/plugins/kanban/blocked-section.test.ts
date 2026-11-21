import { describe, expect, it, vi } from 'vitest'

import type { BlockedTask } from './api'

const view = vi.hoisted(() => ({
  tasks: [] as BlockedTask[],
  rows: [] as { icon: string; label: string; onSelect: () => void }[],
  openSession: vi.fn(),
  navigate: vi.fn()
}))

vi.mock('@hermes/plugin-sdk', async () => ({
  atom: (await import('nanostores')).atom,
  host: { activeConnectionId: () => 'local', navigate: view.navigate, openSession: view.openSession, state: {} },
  SidebarSection: ({ children }: { children: React.ReactNode }) => children,
  SidebarSectionRow: (props: (typeof view.rows)[number]) => {
    view.rows.push(props)

    return null
  },
  useQuery: () => ({ data: view.tasks }),
  useValue: () => true,
  usePluginI18n: vi.fn()
}))
vi.mock('./ui', () => ({
  ago: () => null,
  useKanban: () => ({
    sidebar: {
      blocked: 'Blocked',
      noSession: 'No managing session',
      untitledSession: 'Untitled session',
      openSession: (title: string) => title,
      blockedTip: (title: string) => title
    }
  })
}))

const { groupBlockedTasks, managingSessionRoute, BlockedTasksSection } = await import('./blocked-section')
const { $openTaskRequest } = await import('./open-task')
const { createElement } = await import('react')
const { renderToStaticMarkup } = await import('react-dom/server')

const card = (id: string, manager: BlockedTask['manager'], gateway?: string): BlockedTask =>
  ({ board: 'default', gateway, id, manager, status: 'blocked', title: id }) as BlockedTask

const mgr = (id: string, title: null | string = null, profile: null | string = null) => ({ id, profile, title })

describe('groupBlockedTasks', () => {
  it('groups by managing session in rank order and puts sessionless cards last', () => {
    const groups = groupBlockedTasks([
      card('t1', mgr('s2', 'Second')),
      card('t2', null),
      card('t3', mgr('s1')),
      card('t4', mgr('s2', 'Second')),
      card('t5', mgr('s1', 'First'))
    ])

    expect(groups.map(g => [g.sessionId, g.title, g.tasks.map(t => t.id)])).toEqual([
      ['s2', 'Second', ['t1', 't4']],
      ['s1', 'First', ['t3', 't5']],
      [null, null, ['t2']]
    ])
  })

  it('keeps the same session id on two gateways apart', () => {
    const groups = groupBlockedTasks([card('a', mgr('s', 'x'), 'local'), card('b', mgr('s', 'x'), 'mini')])

    expect(groups.map(g => g.key)).toEqual(['local:s', 'mini:s'])
  })
})

describe('blocked group headers', () => {
  function render(tasks: BlockedTask[]) {
    view.tasks = tasks
    view.rows = []
    view.openSession.mockClear()
    view.navigate.mockClear()
    $openTaskRequest.set(null)
    renderToStaticMarkup(createElement(BlockedTasksSection))

    return view.rows[0]
  }

  it('groups omitted ids by creator and gateway and opens the first card, not a phantom chat', () => {
    const tasks = ['a', 'b'].map(id => ({ ...card(id, null, 'mini'), creator: 'bb-dev' }))
    const groups = groupBlockedTasks(tasks)
    expect(groups).toHaveLength(1)
    expect(groups[0].sessionId).toBeNull()
    const header = render(tasks)
    expect(header.label).toBe('bb-dev · mini')
    expect(header.icon).toBe('circle-slash')
    header.onSelect()
    expect($openTaskRequest.get()).toEqual({ board: 'default', taskId: 'a' })
    expect(view.navigate).toHaveBeenCalledWith('/kanban')
    expect(view.openSession).not.toHaveBeenCalled()
  })

  it('keeps creators and gateways separate and omits the single-gateway suffix', () => {
    const tasks = [
      { ...card('a', null), creator: 'bb-dev' },
      { ...card('b', null), creator: 'bb-device' },
      { ...card('c', null, 'mini'), creator: 'bb-dev' }
    ]

    expect(groupBlockedTasks(tasks)).toHaveLength(3)
    expect(render(tasks.slice(0, 1)).label).toBe('bb-dev')
    expect(render([card('legacy', null)]).label).toBe('No managing session')
  })

  it.each(['failed labels request raw id', 'known id with null title'])(
    '%s retains an Untitled session header that opens the session',
    () => {
      const header = render([card('a', mgr('s'))])
      expect(header.label).toBe('Untitled session')
      expect(header.icon).toBe('comment-discussion')
      header.onSelect()
      expect(view.openSession).toHaveBeenCalledWith('s', {})
      expect(view.navigate).not.toHaveBeenCalled()
    }
  )
})

describe('managingSessionRoute', () => {
  const gateways = [
    { id: 'local', kind: 'local', label: 'Mac' },
    { id: 'mini', kind: 'ssh', label: 'Mini' }
  ]

  it('opens a chat on the routed gateway with a plain open', () => {
    expect(managingSessionRoute({ gateway: 'local', profile: 'default' }, 'local', gateways)).toBeNull()
    expect(managingSessionRoute({ gateway: undefined, profile: null }, 'local', gateways)).toBeNull()
  })

  it('routes a chat on another gateway to that gateway and its profile', () => {
    expect(managingSessionRoute({ gateway: 'mini', profile: 'bb' }, 'local', gateways)).toEqual({
      connectionId: 'mini',
      mode: 'remote',
      profile: 'bb',
      targetProfile: 'bb'
    })
  })

  it('defaults the profile and keeps a local-kind connection local when the active gateway is remote', () => {
    expect(managingSessionRoute({ gateway: 'local', profile: null }, 'mini', gateways)).toEqual({
      connectionId: 'local',
      mode: 'local',
      profile: 'default',
      targetProfile: 'default'
    })
  })
})
