import { afterEach, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'

import { rememberRowTimers, resetRowTimersForTests, rowTimers } from './session-row-timer-memory'

const NOW = 1_800_000_000_000
const cards = [{ id: 'run', title: 'Running', state: 'running' as const, since: NOW / 1000 - 60 }]

const row = (fields: Partial<SessionInfo> = {}) =>
  ({ id: 'retained', profile: 'default', connection_id: 'hub', ...fields }) as SessionInfo

const known = (fields: Partial<SessionInfo> = {}) => row({ kanban_timed_cards: cards, ...fields })
const unknown = (fields: Partial<SessionInfo> = {}) => row({ kanban_unknown: true, ...fields })

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
  resetRowTimersForTests()
})

it('ignores empty identities rather than retaining an anonymous clock', () => {
  vi.useFakeTimers({ now: NOW })
  rememberRowTimers(known({ id: '' }))
  expect(rowTimers(unknown({ id: '' })).cards).toEqual([])
})

it('retains only running and blocked cards in the memo itself', () => {
  vi.useFakeTimers({ now: NOW })
  rememberRowTimers(known({ kanban_timed_cards: [
    ...cards,
    { id: 'blocked', title: 'Blocked', state: 'blocked', since: NOW / 1000 },
    { id: 'waiting', title: 'Waiting', state: 'waiting', since: NOW / 1000 }
  ] }))
  expect(rowTimers(unknown()).cards.map(c => c.state)).toEqual(['running', 'blocked'])
})

it('known zero deletes evidence without allocating an empty retained entry', () => {
  vi.useFakeTimers({ now: NOW })
  rememberRowTimers(known())
  const writes = vi.spyOn(Map.prototype, 'set')
  rememberRowTimers(row())
  expect(writes).not.toHaveBeenCalled()
  expect(rowTimers(unknown()).confirmedAt).toBe(0)
})

it('fresh cards are returned directly before any confirmation effect', () => {
  expect(rowTimers(known({ kanban_timed_more: 3 }))).toEqual({ cards, more: 3, confirmedAt: 0 })
})

it('old observations shorten retention while nonfinite observations use receipt time', () => {
  vi.useFakeTimers({ now: NOW })
  rememberRowTimers(known({ kanban_observed_at: NOW / 1000 - 800 }))
  expect(rowTimers(unknown()).confirmedAt).toBe(NOW - 800_000)
  vi.advanceTimersByTime(100_000)
  expect(rowTimers(unknown()).cards).toEqual([])
  rememberRowTimers(known({ kanban_observed_at: Number.NaN }))
  expect(rowTimers(unknown()).confirmedAt).toBe(NOW + 100_000)
  expect(rowTimers(unknown()).cards).toEqual(cards)
})

it('offline evidence arriving late cannot renew the original confirmation', () => {
  vi.useFakeTimers({ now: NOW })
  rememberRowTimers(known())
  vi.advanceTimersByTime(800_000)
  rememberRowTimers(known({ offline_cached: true }))
  expect(rowTimers(unknown()).cards).toEqual(cards)
  vi.advanceTimersByTime(100_000)
  expect(rowTimers(unknown()).cards).toEqual([])
})

it('a first known-empty read never serves retained evidence before store arrival', () => {
  vi.useFakeTimers({ now: NOW })
  rememberRowTimers(known())
  expect(rowTimers(row()).cards).toEqual([])
  expect(rowTimers(unknown()).cards).toEqual(cards)
})

it('future observation clocks clamp to receipt time and still expire', () => {
  vi.useFakeTimers({ now: NOW })
  rememberRowTimers(known({ kanban_observed_at: NOW / 1000 + 3600 }))
  expect(rowTimers(unknown()).cards).toEqual(cards)
  expect(rowTimers(unknown()).confirmedAt).toBe(NOW)
  vi.advanceTimersByTime(899_999)
  expect(rowTimers(unknown()).cards).toEqual(cards)
  vi.advanceTimersByTime(1)
  expect(rowTimers(unknown()).cards).toEqual([])
})

it('a wall clock stepped back never extends retention', () => {
  vi.useFakeTimers({ now: NOW })
  rememberRowTimers(known())
  vi.setSystemTime(NOW - 3_600_000)
  expect(rowTimers(unknown()).cards).toEqual([])
})

it('sweeps expired entries on the next confirmation, not only at capacity', () => {
  vi.useFakeTimers({ now: NOW })
  rememberRowTimers(known({ id: 'expired' }))
  const deletes = vi.spyOn(Map.prototype, 'delete')
  vi.advanceTimersByTime(900_000)
  rememberRowTimers(known({ id: 'fresh' }))
  expect(deletes).toHaveBeenCalledWith(JSON.stringify(['hub', 'default', 'expired']))
  expect(rowTimers(unknown({ id: 'fresh' })).cards).toEqual(cards)
})

it('caps retained rows at 2000 and refresh moves an existing row to the newest position', () => {
  vi.useFakeTimers({ now: NOW })

  for (let i = 0; i < 2000; i++) {
    rememberRowTimers(known({ id: `row-${i}` }))
  }

  rememberRowTimers(known({ id: 'row-0' }))
  expect(rowTimers(unknown({ id: 'row-1' })).cards).toEqual(cards)
  rememberRowTimers(known({ id: 'overflow' }))
  expect(rowTimers(unknown({ id: 'row-1' })).cards).toEqual([])

  for (const id of ['row-0', 'row-2', 'row-1999', 'overflow']) {
    expect(rowTimers(unknown({ id })).cards).toEqual(cards)
  }
})
