import { cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { I18nProvider } from '@/i18n'

import { SessionRowCardTimers } from './session-row-card-timers'
import { rememberRowTimers, resetRowTimersForTests } from './session-row-timer-memory'

const NOW = 1_800_000_000

const blocked = {
  id: 'shepherd',
  profile: 'default',
  connection_id: 'hub',
  kanban_observed_at: NOW,
  kanban_timed_cards: [{ id: 'default/released', title: 'Enrollment proof', state: 'blocked', since: NOW - 19080 }]
} as SessionInfo

const draw = (session: SessionInfo) =>
  render(
    <I18nProvider configClient={null} initialLocale="en">
      <SessionRowCardTimers session={session} />
    </I18nProvider>
  )

afterEach(() => {
  cleanup()
  resetRowTimersForTests()
  vi.useRealTimers()
})

it.each(['retained', 'explicit-unknown', 'expired'])(
  'never paints %s blocked history red or announces a current human ask',
  source => {
    vi.useFakeTimers({ now: NOW * 1000 })
    rememberRowTimers(blocked)

    const session =
      source === 'retained'
        ? { ...blocked, kanban_unknown: true, kanban_timed_cards: undefined }
        : source === 'explicit-unknown'
          ? { ...blocked, kanban_unknown: true }
          : { ...blocked, kanban_observed_at: NOW - 181 }

    const { container } = draw(session)
    const timer = container.querySelector('[data-card-timer]')
    expect(timer?.textContent).toBe('5h18m')
    expect(timer?.className).toContain('text-muted-foreground')
    expect(container.querySelector('[data-testid="session-row-card-timers"]')?.getAttribute('aria-label')).toContain(
      'unknown'
    )
    expect(container.textContent).not.toContain('Needs you')
    expect(container.querySelector('[aria-label]')?.getAttribute('aria-label')).not.toContain('Needs you')
  }
)

it('does not let an unknown deploy watch withdraw a separate fresh human ask', () => {
  vi.useFakeTimers({ now: NOW * 1000 })

  const { container } = draw({
    ...blocked,
    kanban_external_unknown: 1,
    kanban_timed_cards: [
      ...blocked.kanban_timed_cards!,
      { id: 'default/deploy', title: 'Deploy watch', state: 'waiting', wait_kind: 'deploy', since: NOW - 60 }
    ]
  })

  expect(container.querySelector('[data-card-timer="blocked"]')?.className).toContain('text-red-500')
  expect(container.querySelector('[aria-label]')?.getAttribute('aria-label')).toContain('Needs you')
})

it('keeps a fresh real ask red and replaces its elapsed with the current running clock after release', () => {
  vi.useFakeTimers({ now: NOW * 1000 })
  const first = draw(blocked)
  expect(first.container.querySelector('[data-card-timer]')?.className).toContain('text-red-500')
  expect(first.container.querySelector('[aria-label]')?.getAttribute('aria-label')).toContain('Needs you')
  first.unmount()

  const { container } = draw({
    ...blocked,
    kanban_timed_cards: [{ ...blocked.kanban_timed_cards![0], state: 'running', since: NOW - 60 }]
  })

  expect(container.querySelector('[data-card-timer]')?.className).toContain('text-sky-400')
  expect(container.querySelector('[data-card-timer]')?.textContent).toBe('1m')
  expect(container.querySelector('[aria-label]')?.getAttribute('aria-label')).not.toContain('Needs you')
})
