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

const { groupBlockedTasks } = await import('./blocked-section')

const card = (id: string, manager: BlockedTask['manager'], gateway?: string): BlockedTask =>
  ({ board: 'default', gateway, id, manager, status: 'blocked', title: id }) as BlockedTask

describe('groupBlockedTasks', () => {
  it('groups by managing session in rank order and puts sessionless cards last', () => {
    const groups = groupBlockedTasks([
      card('t1', { id: 's2', title: 'Second' }),
      card('t2', null),
      card('t3', { id: 's1', title: null }),
      card('t4', { id: 's2', title: 'Second' }),
      card('t5', { id: 's1', title: 'First' })
    ])

    expect(groups.map(g => [g.sessionId, g.title, g.tasks.map(t => t.id)])).toEqual([
      ['s2', 'Second', ['t1', 't4']],
      ['s1', 'First', ['t3', 't5']],
      [null, null, ['t2']]
    ])
  })

  it('keeps the same session id on two gateways apart', () => {
    const groups = groupBlockedTasks([card('a', { id: 's', title: 'x' }, 'local'), card('b', { id: 's', title: 'x' }, 'mini')])

    expect(groups.map(g => g.key)).toEqual(['local:s', 'mini:s'])
  })
})
