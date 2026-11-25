import { afterEach, expect, it, vi } from 'vitest'

import { makeSessionInfo } from '@/test/session-info'

import { ownerChatPolicy } from './owner-chats'

const row = (id: string, fields = {}) =>
  makeSessionInfo({
    id: `kanban_owner_${id}`,
    kanban_observed_at: Date.now() / 1000,
    ...fields
  })

afterEach(() => vi.useRealTimers())

it('keeps every unfinished projection, not just running work', () => {
  // ACTIVE_STATUSES includes todo/ready/running/scheduled/review; blocked is separate.
  const rows = [
    row('running', { kanban_active: 1 }),
    row('queued', { kanban_active: 1, kanban_stalled: 1 }),
    row('review', { kanban_active: 1 }),
    row('scheduled', { kanban_active: 1, kanban_parked: 1 }),
    row('todo', { kanban_active: 1, kanban_parked: 1 }),
    row('blocked', { kanban_blocked: 1 }),
    row('held', { kanban_active: 1, kanban_held: 1 })
  ]

  expect(ownerChatPolicy.partition(rows).background).toEqual(rows)
})

it('excludes done history, unseen receipts and unavailable retained counts', () => {
  const rows = [
    row('done', { kanban_unseen: 5, kanban_attention: 2 }),
    row('archived'),
    row('stale', { kanban_active: 1, kanban_observed_at: Date.now() / 1000 - 181 }),
    row('missing', { kanban_active: 1, kanban_observed_at: undefined }),
    row('unknown', { kanban_active: 1, kanban_unknown: true, kanban_observed_at: undefined })
  ]

  expect(ownerChatPolicy.partition(rows).background).toEqual([])
})

it('expires positive counts without modifying ordinary pins or original rows', () => {
  vi.useFakeTimers()
  const open = row('open', { kanban_active: 1 })
  const pin = makeSessionInfo({ id: 'ordinary', kanban_active: 1 })
  expect(ownerChatPolicy.partition([open, pin])).toEqual({ background: [open], pinned: [pin] })
  vi.advanceTimersByTime(181_000)
  expect(ownerChatPolicy.partition([open, pin])).toEqual({ background: [], pinned: [pin] })
  expect(open.kanban_active).toBe(1)
})
