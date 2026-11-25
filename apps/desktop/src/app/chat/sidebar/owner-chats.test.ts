import { expect, it } from 'vitest'

import { makeSessionInfo } from '@/test/session-info'

import { isKanbanOwnerSession, ownerChatPolicy } from './owner-chats'

it('recognizes only the durable owner prefix, including compressed lineage roots', () => {
  expect(isKanbanOwnerSession(makeSessionInfo({ id: 'kanban_owner_runner' }))).toBe(true)
  expect(isKanbanOwnerSession(makeSessionInfo({ id: 'tip', _lineage_root_id: 'kanban_owner_runner' }))).toBe(true)
  expect(isKanbanOwnerSession(makeSessionInfo({ id: 'not_kanban_owner_runner', title: 'Kanban owner: runner' }))).toBe(
    false
  )
})

it('keeps normal pins in order, omits closed owners and sorts every open-card state by activity', () => {
  const fields = ['kanban_active', 'kanban_blocked', 'kanban_held', 'kanban_parked'] as const

  const rows = fields.map((field, i) =>
    makeSessionInfo({
      id: `kanban_owner_${field}`,
      [field]: 1,
      last_active: i + 1,
      kanban_observed_at: Date.now() / 1000
    })
  )

  const normal = makeSessionInfo({ id: 'normal' })
  const closed = makeSessionInfo({ id: 'kanban_owner_closed' })
  const input = [normal, ...rows, closed]
  const { pinned, background } = ownerChatPolicy.partition(input)

  expect(pinned).toEqual([normal])
  expect(background).toEqual([...rows].reverse())
  expect(input).toEqual([normal, ...rows, closed])
})

it('adds only missing owner lineages within each gateway and profile', () => {
  const pin = makeSessionInfo({
    id: 'tip',
    _lineage_root_id: 'kanban_owner_root',
    connection_id: 'A',
    profile: 'default'
  })

  const root = makeSessionInfo({ id: 'kanban_owner_root', connection_id: 'A', profile: 'default' })
  const foreign = { ...root, connection_id: 'B' }
  const otherProfile = { ...root, profile: 'work' }
  const ordinary = makeSessionInfo({ id: 'ordinary' })
  const key = (row: typeof pin) => JSON.stringify([row.connection_id, row.profile])
  expect(ownerChatPolicy.additionalOwners([pin, root, foreign, otherProfile, ordinary], [pin], key)).toEqual([
    foreign,
    otherProfile
  ])
  expect(ownerChatPolicy.additionalOwners([pin], [root], key)).toEqual([])
  expect(ownerChatPolicy.outsideOrdinaryLists(root, () => false)).toBe(true)
  expect(ownerChatPolicy.outsideOrdinaryLists(ordinary, () => true)).toBe(true)
  expect(ownerChatPolicy.outsideOrdinaryLists(ordinary, () => false)).toBe(false)
})
