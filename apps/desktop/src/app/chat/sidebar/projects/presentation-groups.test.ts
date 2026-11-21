import { describe, expect, it } from 'vitest'

import { makeSessionInfo } from '@/test/session-info'

import {
  expandGroupOrder,
  groupProjects,
  groupSortableIds,
  presentationGroupOf,
  projectPresentationOwner
} from './presentation-groups'
import { NO_PROJECT_ID, type SidebarProjectTree } from './workspace-groups'

const node = (id: string, label: string, extra: Partial<SidebarProjectTree> = {}): SidebarProjectTree => ({
  id,
  label,
  path: `/x/${id}`,
  repos: [],
  sessionCount: 1,
  ...extra
})

const home = node(NO_PROJECT_ID, 'Home', { isNoProject: true, path: null })

describe('projectPresentationOwner', () => {
  it('reads connectionId, then the authoritative remoteProject, then all-foreign row stamps', () => {
    expect(projectPresentationOwner(node('a', 'a', { connectionId: 'studio' }))).toBe('studio')
    expect(projectPresentationOwner(node('a', 'a', { remoteProject: { connectionId: 'lab', id: 'p_1' } }))).toBe('lab')

    const stamped = node('a', 'a', {
      previewSessions: [
        makeSessionInfo({ id: '1', connection_id: 'lab', last_active: 1 }),
        makeSessionInfo({ id: '2', connection_id: 'lab', last_active: 2 })
      ]
    })

    expect(projectPresentationOwner(stamped)).toBe('lab')
  })

  it('is the window gateway (null) for bare, mixed, local-stamped or empty nodes', () => {
    expect(projectPresentationOwner(node('a', 'a'))).toBeNull()

    expect(
      projectPresentationOwner(
        node('a', 'a', {
          previewSessions: [makeSessionInfo({ id: '1', connection_id: 'lab' }), makeSessionInfo({ id: '2' })]
        })
      )
    ).toBeNull()

    expect(
      projectPresentationOwner(
        node('a', 'a', { previewSessions: [makeSessionInfo({ id: '1', connection_id: 'local' })] })
      )
    ).toBeNull()
  })

  it('never parses ids or paths', () => {
    expect(projectPresentationOwner(node('gw:studio:p_1', 'a', { path: '/gw:studio/x' }))).toBeNull()
  })
})

describe('groupProjects', () => {
  it('groups non-Home nodes by trimmed, lowercased name; Home stays a singleton', () => {
    const groups = groupProjects([
      home,
      node('a', 'App'),
      node('b', 'infra'),
      node('c', '  app ', { connectionId: 'lab' }),
      node('d', 'Home', { connectionId: 'lab' })
    ])

    expect(groups.map(group => group.memberIds)).toEqual([[NO_PROJECT_ID], ['a', 'c'], ['b'], ['d']])
    expect(groups[0].primary).toBe(home)
  })

  it('picks primary: explicit window, then auto window, then the most recent foreign', () => {
    const lab = node('lab', 'app', { connectionId: 'lab', lastActive: 5 })
    const studio = node('studio', 'app', { connectionId: 'studio', lastActive: 9 })
    const auto = node('auto', 'app', { isAuto: true, lastActive: 1 })
    const explicit = node('explicit', 'app', { lastActive: 0 })

    expect(groupProjects([lab, studio, auto, explicit])[0].primary.id).toBe('explicit')
    expect(groupProjects([lab, studio, auto])[0].primary.id).toBe('auto')

    const [foreignOnly] = groupProjects([lab, studio])

    expect(foreignOnly.primary.id).toBe('studio')
    expect(foreignOnly.memberIds).toEqual(['studio', 'lab'])
  })

  it('orders members primary first, then most recent, and never mutates nodes', () => {
    const nodes = [
      node('a', 'x', { connectionId: 'one', lastActive: 1 }),
      node('b', 'x', { connectionId: 'two', lastActive: 3 }),
      node('c', 'x', { lastActive: 0 }),
      node('d', 'x', { connectionId: 'four', lastActive: 2 })
    ]

    const frozen = JSON.stringify(nodes)
    const [group] = groupProjects(nodes)

    expect(group.memberIds).toEqual(['c', 'b', 'd', 'a'])
    expect(group.members.every(member => nodes.includes(member))).toBe(true)
    expect(JSON.stringify(nodes)).toBe(frozen)
  })

  it('places a group where its first member sat', () => {
    const groups = groupProjects([node('a', 'a'), node('b', 'b'), node('c', 'A', { connectionId: 'lab' })])

    expect(groups.map(group => group.memberIds)).toEqual([['a', 'c'], ['b']])
  })
})

describe('helpers', () => {
  const groups = groupProjects([
    node('a', 'a'),
    node('b', 'b'),
    node('c', 'A', { connectionId: 'lab' }),
    node('d', 'd')
  ])

  it('maps member ids to their group and lists sortable (primary) ids', () => {
    expect(presentationGroupOf(groups, 'c')?.primary.id).toBe('a')
    expect(presentationGroupOf(groups, 'zzz')).toBeUndefined()
    expect(groupSortableIds(groups)).toEqual(['a', 'b', 'd'])
  })

  it('expands a reordered primary list into the flat member order', () => {
    expect(expandGroupOrder(groups, ['d', 'a', 'b'])).toEqual(['d', 'a', 'c', 'b'])
  })
})
