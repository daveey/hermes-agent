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
