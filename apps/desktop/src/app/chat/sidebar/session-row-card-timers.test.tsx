import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { I18nProvider } from '@/i18n'

import { flatRowCardMarks, runningCardCount, sessionCardColours } from './session-row-card-squares'
import { compactElapsed } from './session-row-card-timers'

const NOW = 1_800_000_000

const timer = (id: string, state: 'running' | 'blocked', age: number) => ({
  id,
  title: `Card ${id}`,
  state,
  since: NOW - age
})

const draw = (fields: Partial<SessionInfo>, stripe: Parameters<typeof flatRowCardMarks>[3] = null) =>
  render(
    <I18nProvider configClient={null} initialLocale="en">
      <div data-sidebar-session-id="s">
        {flatRowCardMarks(fields as SessionInfo, 0, <i data-testid="cloud" />, stripe)}
      </div>
    </I18nProvider>
  )

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('removes grey squares but preserves counts and the running-card count', () => {
  const fields = { kanban_active: 3, kanban_parked: 1 }
  const { container } = draw(fields)
  expect(container.querySelector('[data-card-colour="grey"]')).toBeNull()
  expect(sessionCardColours(fields as SessionInfo, 0)).toContainEqual({ colour: 'grey', count: 1 })
  expect(runningCardCount(fields as SessionInfo)).toBe(2)
})

it('orders blue left, red right, oldest first; five clocks and overflow, full tooltip, cloud last', () => {
  vi.useFakeTimers({ now: NOW * 1000 })

  const cards = [
    timer('b', 'blocked', 70),
    timer('a', 'running', 45),
    timer('c', 'running', 50),
    timer('d', 'running', 40),
    timer('e', 'blocked', 65),
    timer('f', 'blocked', 60)
  ]

  const { container, getByTestId } = draw({ kanban_timed_cards: cards, kanban_timed_more: 2 })
  const timers = getByTestId('session-row-card-timers')
  expect(
    [...timers.querySelectorAll('[data-card-timer]')].map(c => [c.getAttribute('data-card-timer'), c.textContent])
  ).toEqual([
    ['running', '50s'],
    ['running', '45s'],
    ['running', '40s'],
    ['blocked', '1m'],
    ['blocked', '1m']
  ])
  expect(container.querySelector('[data-card-timer="running"]')?.className).toContain('text-sky-400')
  expect(container.querySelector('[data-card-timer="blocked"]')?.className).toContain('text-red-500')
  expect(container.querySelector('[data-timer-overflow]')?.textContent).toBe('…')

  for (const c of cards) {
    expect(timers.getAttribute('aria-label')).toContain(c.title)
  }

  expect(timers.getAttribute('aria-label')).toContain('Running · 45s')
  expect(timers.getAttribute('aria-label')).toContain('Needs you · 1m')
  expect(timers.getAttribute('aria-label')).toContain('… +2')
  expect(timers.nextElementSibling).toBe(getByTestId('cloud'))
  expect(getByTestId('cloud').nextElementSibling).toBeNull()
})

it('does not start a display clock on rows without timed cards', () => {
  vi.useFakeTimers({ now: NOW * 1000 })
  const before = vi.getTimerCount()
  draw({})
  expect(vi.getTimerCount()).toBe(before)
})

it('ticks locally across seconds/minutes without any backend read', () => {
  vi.useFakeTimers({ now: NOW * 1000 })
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  const { container } = draw({ kanban_timed_cards: [timer('a', 'running', 59)] })
  expect(container.querySelector('[data-card-timer]')?.textContent).toBe('59s')
  act(() => vi.advanceTimersByTime(1000))
  expect(container.querySelector('[data-card-timer]')?.textContent).toBe('1m')
  expect(fetch).not.toHaveBeenCalled()
})

it('shrinks its timer budget on a narrow row without dropping tooltip details', () => {
  vi.useFakeTimers({ now: NOW * 1000 })

  let resize: ResizeObserverCallback = () => {}
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(callback: ResizeObserverCallback) {
        resize = callback
      }
      observe() {}
      disconnect() {}
    }
  )
  const cards = Array.from({ length: 6 }, (_, i) => timer(`${i}`, 'running', 3900))
  const { container, getByTestId } = draw({ kanban_timed_cards: cards })
  act(() => resize([{ contentRect: { width: 120 } } as ResizeObserverEntry], {} as ResizeObserver))
  expect(container.querySelectorAll('[data-card-timer]').length).toBe(1)
  expect(container.querySelector('[data-timer-overflow]')).not.toBeNull()

  for (const c of cards) {
    expect(getByTestId('session-row-card-timers').getAttribute('aria-label')).toContain(c.title)
  }
})

it('keeps ETA and other coloured marks alongside elapsed clocks with the cloud last', () => {
  vi.useFakeTimers({ now: NOW * 1000 })

  const fields: Partial<SessionInfo> = {
    kanban_timed_cards: [timer('a', 'running', 45)],
    kanban_held: 1,
    kanban_attention: 1,
    kanban_stalled: 1,
    kanban_unseen: 1,
    kanban_external_cards: [
      {
        task_id: 'ci',
        external: { service: 'CI', health: 'ok', job: 'running' },
        eta: { wait_kind: 'clock', waited_since: NOW - 60, expected_s: 120 }
      }
    ]
  }

  const first = draw(fields)

  for (const colour of ['pink', 'amber', 'green', 'violet']) {
    expect(first.container.querySelector(`[data-card-colour="${colour}"]`)).not.toBeNull()
  }

  first.unmount()
  const { getByTestId } = draw(fields, 'external')
  expect(getByTestId('session-row-eta').nextElementSibling).toBe(getByTestId('session-row-card-timers'))
  expect(getByTestId('session-row-card-timers').nextElementSibling).toBe(getByTestId('cloud'))
})

it.each([
  [45, '45s'],
  [780, '13m'],
  [3900, '1h5m'],
  [172800, '2d'],
  [-1, '0s']
])('formats %s compactly', (s, text) => {
  expect(compactElapsed(Number(s))).toBe(text)
})
