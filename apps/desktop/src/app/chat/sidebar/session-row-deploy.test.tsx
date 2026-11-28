import { cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { I18nProvider } from '@/i18n'

import { aggregateCardColours, flatRowCardMarks, sessionCardColours } from './session-row-card-squares'
import { allDeployWaits, deployCardCount } from './session-row-deploy'
import { SessionRowStatusStripe } from './session-row-stripe'
import { rememberRowTimers, rowTimers } from './session-row-timer-memory'

const NOW = 1_800_000_000

const row = (fields: Partial<SessionInfo> = {}): SessionInfo =>
  ({
    kanban_active: 1,
    kanban_parked: 1,
    kanban_external: 1,
    kanban_observed_at: NOW,
    kanban_external_cards: [
      { task_id: 'deploy-card', external: { adapter: 'deploy', service: 'deploy', health: 'unknown', job: 'unknown' } }
    ],
    ...fields
  }) as SessionInfo

const draw = (session: SessionInfo, stripe: 'external' | 'external_down' | null = null) =>
  render(
    <I18nProvider configClient={null} initialLocale="en">
      {flatRowCardMarks(session, 0, null, stripe)}
      <SessionRowStatusStripe outcomePainted={false} session={session} statusStripe={stripe} />
    </I18nProvider>
  )

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

it.each([
  ['waiting', 'deploy', false, false, 'text-[#a371f7]'],
  ['waiting', 'deploy', true, false, 'text-[#a371f7]'],
  ['waiting', 'deploy', false, true, 'text-[#a371f7]'],
  ['waiting', 'ci', false, false, 'text-sky-400'],
  ['waiting', 'clock', false, false, 'text-sky-400'],
  ['running', 'deploy', false, false, 'text-sky-400'],
  ['blocked', 'deploy', false, false, 'text-red-500']
] as const)('elapsed %s/%s unknown=%s stale=%s has the right colour', (state, kind, unknown, stale, colour) => {
  vi.useFakeTimers({ now: NOW * 1000 })

  const session = row({
    id: 'timed-deploy',
    kanban_unknown: unknown,
    kanban_observed_at: NOW - (stale ? 240 : 0),
    kanban_timed_cards: [{ id: 'timed', title: 'Ship', state, since: NOW - 900, wait_kind: kind }]
  })

  const { container } = render(
    <I18nProvider configClient={null} initialLocale="en">
      {flatRowCardMarks(session, 0, null)}
    </I18nProvider>
  )

  const timer = container.querySelector(`[data-card-timer="${state}"]`) as HTMLElement
  expect(timer?.textContent).toBe('15m')
  expect(timer.className).toContain(colour)

  if (state === 'waiting' && kind === 'deploy') {
    expect(timer.getAttribute('data-wait-kind')).toBe('deploy')
    expect(timer.parentElement?.getAttribute('aria-label')).toContain('Waiting on deploy')

    if (unknown || stale) {
      expect(timer.parentElement?.className).toContain('opacity-60')
      expect(timer.parentElement?.getAttribute('aria-label')).toMatch(/last confirmed/i)
    }
  }
})

it('retains only bounded last-confirmed deploy clocks, not other waiting categories', () => {
  vi.useFakeTimers({ now: NOW * 1000 })

  const cards: NonNullable<SessionInfo['kanban_timed_cards']> = [
    { id: 'deploy', title: 'Ship', state: 'waiting', since: NOW - 900, wait_kind: 'deploy' },
    { id: 'ci', title: 'Check', state: 'waiting', since: NOW - 900, wait_kind: 'ci' }
  ]

  const fresh = row({ id: 'retained-deploy', kanban_timed_cards: cards })
  rememberRowTimers(fresh)
  expect(rowTimers({ ...fresh, kanban_unknown: true, kanban_timed_cards: undefined }).cards).toEqual([cards[0]])
  expect(rowTimers({ ...fresh, kanban_external_unknown: 1 }).confirmedAt).toBe(NOW * 1000)
  expect(rowTimers({ ...fresh, kanban_external_unknown: 1 }).cards).toEqual(cards)

  for (const age of [900, -1]) {
    expect(rowTimers({ ...fresh, kanban_unknown: true, kanban_observed_at: NOW - age }).cards).toEqual([])
  }

  expect(rowTimers({ ...fresh, kanban_unknown: true, kanban_observed_at: undefined }).cards).toEqual([])
  expect(rowTimers({ ...fresh, offline_cached: true }).cards).toEqual([])
})

it('does not overcount duplicate deploy cards, repaint mixed waits, or create counts without open work', () => {
  vi.useFakeTimers({ now: NOW * 1000 })
  const session = row()
  const card = session.kanban_external_cards![0]
  expect(deployCardCount({ ...session, kanban_external_cards: [card, card] })).toBe(1)
  expect(allDeployWaits({ ...session, kanban_active: 2 })).toBe(false)
  expect(allDeployWaits({ ...session, kanban_external_down: 1 })).toBe(false)
  expect(sessionCardColours({ ...session, kanban_active: 0, kanban_parked: 0 }, 0)).toEqual([])
  expect(aggregateCardColours([{ ...session, kanban_observed_at: NOW - 181 }])).toEqual([
    { colour: 'unknown', count: 1 }
  ])
})

it('shows a purple deploy square without an ETA, labels it and preserves unseen violet', () => {
  vi.useFakeTimers({ now: NOW * 1000 })
  const { container } = draw(row({ kanban_unseen: 1 }), 'external')
  expect(container.querySelector('[data-card-colour="deploy"]')?.className).toContain('bg-[#a371f7]')
  expect(container.querySelector('[data-card-colour="violet"]')?.className).toContain('bg-violet-500')
  expect(container.querySelector('[data-testid="session-row-card-squares"]')?.getAttribute('aria-label')).toBe(
    'Waiting on deploy 1 · Done, not yet seen 1'
  )
  expect(container.querySelector('[data-slot="session-status-stripe"]')?.getAttribute('style')).toContain('#a371f7')
})

it.each([0, 1])('reclassifies deploy from live/parked without adding counts (parked=%s)', parked => {
  vi.useFakeTimers({ now: NOW * 1000 })
  const session = row({ kanban_parked: parked })
  expect(sessionCardColours(session, 0)).toEqual([{ colour: 'deploy', count: 1 }])
  expect(aggregateCardColours([session, session])).toEqual([{ colour: 'deploy', count: 2 }])

  const { container } = render(
    <I18nProvider configClient={null} initialLocale="en">
      {flatRowCardMarks(session, 0, null, null, true)}
    </I18nProvider>
  )

  expect(container.querySelector('[data-card-colour="deploy"]')?.textContent).toBe('1')
  expect(container.querySelector('[data-card-colour="deploy"]')?.className).toContain('border-[#a371f7]')
})

it.each(['deploy', 'ci', 'clock'])('colours the live %s ETA by its kind', kind => {
  vi.useFakeTimers({ now: NOW * 1000 })

  const session = row({
    kanban_external_cards: [
      {
        task_id: 'wait-card',
        eta: { wait_kind: kind, waited_since: NOW - 60, expected_s: 600 },
        external: { adapter: 'shell', service: 'producer', health: 'ok', job: 'running' }
      }
    ]
  })

  const { container } = draw(session, 'external')
  const left = container.querySelector('[data-eta="left"]') as HTMLElement
  expect(left.textContent).toBe('9m')

  if (kind === 'deploy') {
    expect(left.style.color).toBe('rgb(163, 113, 247)')
  } else {
    expect(left.className).toContain('text-sky-400')
  }
})

it('keeps down deploy evidence out of purple squares, ETA and stripe', () => {
  vi.useFakeTimers({ now: NOW * 1000 })

  const session = row({
    kanban_external: 0,
    kanban_external_down: 1,
    kanban_timed_cards: [
      { id: 'default/down-card', title: 'Down deploy', state: 'waiting', since: NOW - 60, wait_kind: 'deploy' }
    ],
    kanban_external_cards: [
      {
        task_id: 'down-card',
        eta: { wait_kind: 'deploy', waited_since: NOW - 60, expected_s: 600 },
        external: { adapter: 'deploy', service: 'deploy', health: 'down', job: 'running' }
      }
    ]
  })

  expect(sessionCardColours(session, 0)).toEqual([{ colour: 'grey', count: 1 }])
  const { container } = draw(session, 'external_down')
  expect(container.querySelector('[data-card-colour="deploy"], [data-eta="left"]')).toBeNull()
  expect(container.querySelector('[data-card-timer="waiting"]')?.className).toContain('text-red-500')
  expect(container.querySelector('[data-slot="session-status-stripe"]')?.getAttribute('style')).toBeNull()
})
