import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { I18nProvider } from '@/i18n'

import { flatRowCardMarks } from './session-row-card-squares'

const NOW = 1_800_000_000

const card = (adapter: string, eta: object | null, extra: object = {}) => ({
  eta,
  external: { adapter, health: 'ok', job: 'running', service: adapter === 'clock' ? 'timer' : 'GitHub Actions' },
  task_id: `t_${adapter}`,
  title: adapter,
  ...extra
})

const row = (fields: Partial<SessionInfo>): SessionInfo =>
  ({ kanban_parked: 1, kanban_stalled: 0, ...fields }) as SessionInfo

const draw = (session: SessionInfo, stripe: Parameters<typeof flatRowCardMarks>[3] = 'external') =>
  render(
    <I18nProvider configClient={null} initialLocale="en">
      {flatRowCardMarks(session, 0, <i data-testid="gateway-cloud" />, stripe)}
    </I18nProvider>
  )

const text = (el: Element | null) => Array.from(el?.querySelectorAll('[data-eta]') ?? [], c => c.textContent).join(' ')

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('the ETA readout on light-blue rows', () => {
  it.each([
    ['ci', 'CI'],
    ['deploy', 'deploy'],
    ['clock', 'timer']
  ])('replaces the squares on a %s wait and keeps the gateway cloud last', (kind, label) => {
    vi.useFakeTimers({ now: NOW * 1000 })
    const eta = { estimate_source: 'basis', expected_s: 25 * 60, wait_kind: kind, waited_since: NOW - 12 * 60 }
    const adapter = kind === 'clock' ? 'clock' : 'github_run'

    const { container, getByTestId, queryByTestId } = draw(
      row({ kanban_external: 1, kanban_external_cards: [card(adapter, eta)], kanban_stalled: 1 })
    )

    const readout = getByTestId('session-row-eta')

    expect(text(readout)).toBe('12m / 25m 13m')
    expect(readout.getAttribute('aria-label')).toBe(
      `Waiting on ${label} · waited 12m · expected 25m (basis) · 13m left`
    )
    expect(queryByTestId('session-row-card-squares')).toBeNull()
    // the cloud stays the last mark of the cluster
    expect(container.querySelector('[data-testid="gateway-cloud"]')?.previousElementSibling).toBe(readout)
    expect(container.querySelector('[data-testid="gateway-cloud"]')?.nextElementSibling).toBeNull()
  })

  it.each(['ci', 'deploy', 'clock'])('renders the replica projection shape for %s', kind => {
    vi.useFakeTimers({ now: NOW * 1000 })
    const clock = kind === 'clock'

    const projected = {
      eta: {
        estimate_source: clock ? 'wakes 14:30' : 'no history yet',
        expected_s: clock ? 1500 : null,
        wait_kind: kind,
        waited_since: NOW - 720
      },
      external: {
        adapter: kind,
        health: clock ? 'ok' : 'unknown',
        job: clock ? 'running' : 'unknown',
        state: clock ? 'external_running' : 'external_unknown'
      },
      task_id: 'edge/default/default/t_wait',
      title: 'Remote wait'
    }

    const { getByTestId, queryByTestId } = draw(
      row({
        kanban_external: 1,
        kanban_external_cards: [projected],
        kanban_external_unknown: clock ? 0 : 1
      })
    )

    expect(text(getByTestId('session-row-eta'))).toBe(clock ? '12m / 25m 13m' : '12m / ? —')
    expect(queryByTestId('session-row-card-squares')).toBeNull()
  })

  it('keeps the squares on a row that is not light blue', () => {
    const eta = { expected_s: 600, wait_kind: 'ci', waited_since: NOW - 60 }

    const session = row({
      kanban_external: 1,
      kanban_external_cards: [card('github_run', eta)],
      kanban_stalled: 1,
      kanban_parked: 2
    })

    for (const stripe of ['waiting', 'stalled', null] as const) {
      const { queryByTestId, unmount } = draw(session, stripe)

      expect(queryByTestId('session-row-eta')).toBeNull()
      expect(queryByTestId('session-row-card-squares')).not.toBeNull()
      unmount()
    }
  })

  it('keeps the squares when the light-blue row carries no ETA (a replica, an older gateway)', () => {
    const { queryByTestId } = draw(
      row({ kanban_external: 1, kanban_external_cards: [card('github_run', null)], kanban_stalled: 1 })
    )

    expect(queryByTestId('session-row-eta')).toBeNull()
    expect(queryByTestId('session-row-card-squares')).not.toBeNull()
  })

  it('paints an over-estimate +over in orange', () => {
    vi.useFakeTimers({ now: NOW * 1000 })
    const eta = { expected_s: 25 * 60, wait_kind: 'ci', waited_since: NOW - 41 * 60 }

    const { getByTestId } = draw(row({ kanban_external: 1, kanban_external_cards: [card('github_run', eta)] }))
    const readout = getByTestId('session-row-eta')
    const left = readout.querySelector('[data-eta="left"]') as HTMLElement

    expect(text(readout)).toBe('41m / 25m +16m')
    expect(readout.getAttribute('data-over')).toBe('true')
    expect(left.style.color).toBe('rgb(240, 136, 62)') // #f0883e
    expect(left.className).not.toContain('sky')
  })

  it('reads <waited> / ? and a dash without an estimate, in the neutral colour', () => {
    vi.useFakeTimers({ now: NOW * 1000 })
    const eta = { estimate_source: 'no history yet', expected_s: null, wait_kind: 'ci', waited_since: NOW - 3.4 * 3600 }

    const { getByTestId } = draw(row({ kanban_external: 1, kanban_external_cards: [card('github_run', eta)] }))
    const readout = getByTestId('session-row-eta')

    expect(text(readout)).toBe('3.4h / ? —')
    expect((readout.querySelector('[data-eta="left"]') as HTMLElement).className).not.toContain('sky')
  })

  it('counts down live', () => {
    vi.useFakeTimers({ now: NOW * 1000 })
    const eta = { expected_s: 25 * 60, wait_kind: 'ci', waited_since: NOW - 12 * 60 }
    const { getByTestId } = draw(row({ kanban_external: 1, kanban_external_cards: [card('github_run', eta)] }))

    expect(text(getByTestId('session-row-eta'))).toBe('12m / 25m 13m')
    act(() => {
      vi.advanceTimersByTime(3 * 60 * 1000)
    })
    expect(text(getByTestId('session-row-eta'))).toBe('15m / 25m 10m')
  })

  it('leads with the wait that ends soonest and lists every wait in the title', () => {
    vi.useFakeTimers({ now: NOW * 1000 })
    const slow = { expected_s: 4 * 3600, wait_kind: 'clock', waited_since: NOW - 3600 }
    const soon = { expected_s: 25 * 60, wait_kind: 'ci', waited_since: NOW - 12 * 60 }

    const { getByTestId } = draw(
      row({ kanban_external: 2, kanban_external_cards: [card('clock', slow), card('github_run', soon)] })
    )

    const readout = getByTestId('session-row-eta')

    expect(text(readout)).toBe('12m / 25m 13m')
    expect(readout.getAttribute('aria-label')?.split('\n')).toHaveLength(2)
  })
})
