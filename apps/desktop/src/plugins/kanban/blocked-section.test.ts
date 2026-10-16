import { describe, expect, it, vi } from 'vitest'

import type { BlockedTask } from './api'

vi.mock('@hermes/plugin-sdk', async () => ({
  atom: (await import('nanostores')).atom,
  host: {},
  SidebarSection: () => null,
  SidebarSectionRow: () => null,
  useQuery: vi.fn(),
  useValue: vi.fn(),
  usePluginI18n: vi.fn()
}))

const { groupBlockedTasks, managingSessionRoute } = await import('./blocked-section')

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
