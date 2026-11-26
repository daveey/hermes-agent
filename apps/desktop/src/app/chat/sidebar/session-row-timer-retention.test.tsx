import { act, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { I18nProvider } from '@/i18n'

import { SessionRowCardTimers } from './session-row-card-timers'

const NOW = 1_800_000_000
afterEach(() => vi.useRealTimers())

const row = (fields: Partial<SessionInfo> = {}) =>
  ({ id: 'retained', profile: 'default', connection_id: 'hub', ...fields }) as SessionInfo

const cards = [
  { id: 'run', title: 'Running card', state: 'running' as const, since: NOW - 60 },
  { id: 'block', title: 'Blocked card', state: 'blocked' as const, since: NOW - 120 },
  { id: 'wait', title: 'Waiting card', state: 'waiting' as const, since: NOW - 180 }
]

const view = (session: SessionInfo) => (
  <I18nProvider configClient={null} initialLocale="en">
    <SessionRowCardTimers session={session} />
  </I18nProvider>
)

it('retains running/blocked timers and overflow dimmed on unknown, never waiting; survives remount', () => {
  vi.useFakeTimers({ now: NOW * 1000 })
  const first = render(view(row({ kanban_observed_at: NOW, kanban_timed_cards: cards, kanban_timed_more: 2 })))
  first.unmount()
  act(() => vi.advanceTimersByTime(120_000))
  const next = render(view(row({ kanban_unknown: true })))
  const timers = next.getByTestId('session-row-card-timers')
  expect(timers.className).toContain('opacity-60')
  expect(timers.getAttribute('aria-label')).toContain('last confirmed 2m ago')
  expect(timers.getAttribute('aria-label')).toContain('… +2')
  expect(next.container.querySelectorAll('[data-card-timer]')).toHaveLength(2)
  expect(next.container.querySelector('[data-card-timer="waiting"]')).toBeNull()
  act(() => vi.advanceTimersByTime(780_000))
  expect(next.container.querySelector('[data-card-timer]')).toBeNull()
})

it.each([{ kanban_timed_cards: undefined }, { kanban_timed_cards: [] }])(
  'known empty clears retained clocks: %j',
  empty => {
    vi.useFakeTimers({ now: NOW * 1000 })
    const ui = render(view(row({ kanban_timed_cards: cards })))
    ui.rerender(view(row({ kanban_unknown: true })))
    expect(ui.container.querySelectorAll('[data-card-timer]')).toHaveLength(2)
    ui.rerender(view(row(empty)))
    ui.rerender(view(row({ kanban_unknown: true })))
    expect(ui.container.querySelector('[data-card-timer]')).toBeNull()
  }
)

it('offline rows neither display nor confirm clocks, and an unknown update does not renew retention', () => {
  vi.useFakeTimers({ now: NOW * 1000 })
  const ui = render(view(row({ kanban_timed_cards: cards })))
  ui.rerender(view(row({ offline_cached: true, kanban_timed_cards: cards })))
  expect(ui.container.querySelector('[data-card-timer]')).toBeNull()
  act(() => vi.advanceTimersByTime(600_000))
  ui.rerender(view(row({ kanban_unknown: true })))
  expect(ui.container.querySelectorAll('[data-card-timer]')).toHaveLength(2)
  act(() => vi.advanceTimersByTime(300_000))
  expect(ui.container.querySelector('[data-card-timer]')).toBeNull()
})

it.each([{ connection_id: 'other' }, { profile: 'other' }, { id: 'other' }])('isolates row identity: %j', identity => {
  vi.useFakeTimers({ now: NOW * 1000 })
  const ui = render(view(row({ kanban_timed_cards: cards })))
  ui.rerender(view(row({ ...identity, kanban_unknown: true })))
  expect(ui.container.querySelector('[data-card-timer]')).toBeNull()
})

it('does not extend last confirmation on local authoritative-row ticks', () => {
  vi.useFakeTimers({ now: NOW * 1000 })
  const ui = render(view(row({ kanban_timed_cards: cards })))
  act(() => vi.advanceTimersByTime(890_000))
  ui.rerender(view(row({ kanban_unknown: true })))
  expect(ui.container.querySelectorAll('[data-card-timer]')).toHaveLength(2)
  act(() => vi.advanceTimersByTime(10_000))
  expect(ui.container.querySelector('[data-card-timer]')).toBeNull()
})
