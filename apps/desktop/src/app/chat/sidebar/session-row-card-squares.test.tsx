import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { I18nProvider } from '@/i18n'
import { $kanbanObservationTick } from '@/store/kanban-observation'

import {
  aggregateCardColours,
  BackgroundCardSquares,
  flatRowCardMarks,
  runningCardCount,
  sessionCardColours,
  SessionRowCardSquares
} from './session-row-card-squares'

const session = (fields: Partial<SessionInfo> = {}) => fields as SessionInfo

it('refreshes retained-state badges on ticks with stable props and no parent rerender', () => {
  vi.useFakeTimers()
  const sessions = [session({ kanban_active: 2, kanban_observed_at: Date.now() / 1000 })]
  const { container } = render(<BackgroundCardSquares sessions={sessions} />)
  expect(container.querySelector('[data-card-colour="blue"]')?.textContent).toBe('2')
  act(() => {
    vi.setSystemTime(Date.now() + 181_000)
    $kanbanObservationTick.set($kanbanObservationTick.get() + 1)
  })
  expect(container.querySelector('[data-card-colour="blue"]')).toBeNull()
  expect(container.querySelector('[data-card-colour="unknown"]')?.textContent).toBe('2')
})

it.each(['expired', 'unstamped', 'future'] as const)(
  'keeps %s asks separate from unknown work and excludes completed receipts on row and header',
  stamp => {
    vi.useFakeTimers()
    const now = Date.now() / 1000

    const row = session({
      kanban_observed_at: stamp === 'unstamped' ? undefined : now + (stamp === 'expired' ? -181 : 3600),
      kanban_active: 3,
      kanban_held: 1,
      kanban_blocked: 9,
      needs_you: [{ kind: 'card', question: 'Go?', ref: 'ask' }],
      kanban_attention: 2,
      kanban_unseen: 5
    })

    expect(aggregateCardColours([row])).toEqual([
      { colour: 'red', count: 1 },
      { colour: 'unknown', count: 3 }
    ])

    const { container } = render(
      <I18nProvider configClient={null} initialLocale="en">
        <div data-testid="row-marks">{flatRowCardMarks(row, 0, null, undefined, true)}</div>
        <BackgroundCardSquares sessions={[row, { ...row, needs_you: [], kanban_active: 0, kanban_held: 0 }]} />
      </I18nProvider>
    )

    const marks = container.querySelectorAll('[data-testid="session-row-card-squares"]')
    expect(marks).toHaveLength(2)

    for (const mark of marks) {
      expect(mark.getAttribute('aria-label')).toBe('Needs you 1 · Unknown 3')
      expect(mark.querySelector('[data-card-colour="red"]')?.textContent).toBe('1')
      expect(mark.querySelector('[data-card-colour="unknown"]')?.textContent).toBe('3')
      expect(
        mark.querySelector('[data-card-colour="amber"], [data-card-colour="violet"], [data-card-colour="grey"]')
      ).toBeNull()
    }
  }
)

const draw = (fields: Partial<SessionInfo> = {}, executionUnknown = 0) =>
  render(
    <I18nProvider configClient={null} initialLocale="en">
      <SessionRowCardSquares executionUnknown={executionUnknown} session={session(fields)} />
    </I18nProvider>
  )

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('sessionCardColours', () => {
  it('counts backend overdue triage once on ordinary and Background rows, without needs_you', () => {
    const fields = {
      kanban_active: 1,
      kanban_parked: 1,
      kanban_stalled: 1,
      needs_you: [],
      kanban_observed_at: Date.now() / 1000,
      kanban_triage_attention: { count: 1, refs: ['owner/default/work/t_card'], parked: 1, held: 0, stalled: 1 }
    }

    expect(sessionCardColours(session(fields), 0)).toEqual([{ colour: 'red', count: 1 }])
    expect(aggregateCardColours([session(fields)])).toEqual([{ colour: 'red', count: 1 }])
    const { container } = draw(fields)
    expect(container.querySelector('[data-card-colour="red"]')).not.toBeNull()
    expect(container.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe('Needs attention 1')
    const mounted = render(<BackgroundCardSquares sessions={[session(fields)]} />)
    expect(mounted.container.querySelector('[data-card-colour="red"]')?.textContent).toBe('1')
  })
  it('labels a human ask and an overdue triage card separately in one red count', () => {
    const fields = {
      kanban_active: 1,
      kanban_parked: 1,
      kanban_stalled: 0,
      kanban_observed_at: Date.now() / 1000,
      needs_you: [{ kind: 'card', question: 'Go?', ref: 'owner/default/work/t_ask' }],
      kanban_triage_attention: { count: 1, refs: ['owner/default/work/t_card'], parked: 1, held: 0, stalled: 0 }
    }

    expect(sessionCardColours(session(fields), 0)).toEqual([{ colour: 'red', count: 2 }])
    const { container } = draw(fields)
    expect(container.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe('Needs you 1 · Needs attention 1')
    cleanup()
    const mounted = render(<BackgroundCardSquares sessions={[session(fields)]} />)
    expect(mounted.container.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe(
      'Needs you 1 · Needs attention 1'
    )
  })
  it.each(['expired', 'unstamped', 'future'] as const)('overdue attention needs a fresh observation: %s', stamp => {
    const now = Date.now() / 1000

    const value = session({
      kanban_active: 1,
      kanban_parked: 1,
      kanban_observed_at: stamp === 'unstamped' ? undefined : now + (stamp === 'expired' ? -181 : 3600),
      needs_you: [],
      kanban_triage_attention: { count: 1, refs: ['owner/default/work/t_card'], parked: 1, held: 0, stalled: 0 }
    })

    expect(aggregateCardColours([value])).toEqual([{ colour: 'unknown', count: 1 }])
  })
  it('red uses needs_you when present, including an empty array, otherwise blocked', () => {
    expect(sessionCardColours(session({ kanban_blocked: 2 }), 0)).toEqual([{ colour: 'red', count: 2 }])
    expect(
      sessionCardColours(session({ kanban_blocked: 9, needs_you: [{ kind: 'card', question: 'Go?', ref: 'x' }] }), 0)
    ).toEqual([{ colour: 'red', count: 1 }])
    expect(sessionCardColours(session({ kanban_blocked: 9, needs_you: [] }), 0)).toEqual([])
  })
  it('pink counts held cards', () => {
    expect(sessionCardColours(session({ kanban_held: 2 }), 0)).toEqual([{ colour: 'pink', count: 2 }])
  })
  it('orange marks stalled work while amber retains completed-card attention', () => {
    expect(sessionCardColours(session({ kanban_stalled: 2, kanban_attention: 1 }), 0)).toEqual([
      { colour: 'orange', count: 2 },
      { colour: 'amber', count: 1 }
    ])
  })
  it('blue subtracts parked and held, clamps to zero, and accepts legacy missing parked', () => {
    expect(sessionCardColours(session({ kanban_active: 7, kanban_parked: 2, kanban_held: 1 }), 0)).toContainEqual({
      colour: 'blue',
      count: 4
    })
    expect(sessionCardColours(session({ kanban_active: 1, kanban_parked: 2 }), 0)).not.toContainEqual({
      colour: 'blue',
      count: -1
    })
    expect(sessionCardColours(session({ kanban_active: 2 }), 0)).toEqual([{ colour: 'blue', count: 2 }])
  })
  it('grey combines nonstalled parked, unknown evidence and kanban_unknown', () => {
    expect(
      sessionCardColours(session({ kanban_parked: 4, kanban_stalled: 1, kanban_unknown: true }), 2)
    ).toContainEqual({ colour: 'grey', count: 6 })
    expect(sessionCardColours(session({ kanban_parked: 1, kanban_stalled: 3 }), 1)).toContainEqual({
      colour: 'grey',
      count: 1
    })
  })
  it('violet counts unseen results, never worker waits', () => {
    expect(sessionCardColours(session({ kanban_unseen: 2 }), 0)).toEqual([{ colour: 'violet', count: 2 }])
  })
  it.each(['run_id', 'generation'] as const)('refines live counts using current queued-card %s evidence', attempt => {
    vi.useFakeTimers()
    vi.setSystemTime(1000 * 1000)

    const queued = {
      version: 1,
      identity: { gateway: 'edge', profile: 'work', board: 'tasks', kind: 'card', work_id: 'card-a', [attempt]: 7 },
      owner: { gateway: 'edge', profile: 'work' },
      state: 'queued',
      source: 'kanban.card',
      source_revision: 'event:42',
      observed_at: 1000,
      valid_until: 1180,
      evidence_refs: ['event:42']
    }

    const row = session({
      kanban_active: 3,
      execution_observations: [
        queued,
        queued,
        null,
        { ...queued, identity: { ...queued.identity, kind: 'turn', session: 'chat' } }
      ]
    })

    expect(sessionCardColours(row, 0)).toEqual([
      { colour: 'green', count: 1 },
      { colour: 'blue', count: 2 }
    ])
    expect(sessionCardColours(session({ ...row, kanban_active: 0 }), 0)).toEqual([])
    expect(
      sessionCardColours(session({ ...row, execution_observations: [queued, { ...queued, state: 'running' }] }), 0)
    ).toEqual([{ colour: 'blue', count: 3 }])
    vi.setSystemTime(1180 * 1000)
    expect(sessionCardColours(row, 0)).toEqual([{ colour: 'blue', count: 3 }])
  })
  it('keeps urgency order regardless of input property order', () => {
    expect(
      sessionCardColours(
        session({
          kanban_unseen: 1,
          kanban_parked: 2,
          kanban_active: 5,
          kanban_stalled: 1,
          kanban_held: 1,
          kanban_blocked: 1
        }),
        0
      ).map(item => item.colour)
    ).toEqual(['red', 'pink', 'orange', 'blue', 'grey', 'violet'])
  })
})

it('renders one 6px square for two cards of the same colour', () => {
  const { container } = draw({ kanban_blocked: 2 })
  expect(container.querySelectorAll('[data-card-colour="red"]')).toHaveLength(1)
  expect(container.querySelector('[data-card-colour="red"]')?.className).toContain('size-[6px]')
  expect(screen.getByRole('img').getAttribute('aria-label')).toBe('Needs you 2')
})

it('keeps running cards out of the trailing cluster and empty flat marks', () => {
  const row = session({ kanban_active: 2 })
  const { container } = draw(row)
  expect(container.querySelector('[data-card-colour="blue"]')).toBeNull()
  expect(screen.queryByTestId('session-row-card-squares')).toBeNull()
  expect(flatRowCardMarks(row, 0, null)).toBeNull()
  expect(flatRowCardMarks(row, 0, <span>Gateway</span>)).not.toBeNull()
})

it.each([
  [{}, 0],
  [{ kanban_active: 2 }, 2],
  [{ kanban_active: 7, kanban_parked: 2, kanban_held: 1 }, 4],
  [{ kanban_active: 1, kanban_parked: 2 }, 0]
] as const)('supplies the merged circle count for %j', (fields, count) => {
  expect(runningCardCount(session(fields))).toBe(count)
})

it('renders worker waits and unseen completions with distinct colours and copy', () => {
  vi.useFakeTimers()
  const { container } = draw({ kanban_active: 2, kanban_parked: 2, kanban_stalled: 2, kanban_unseen: 1 })
  expect(container.querySelector('[data-card-colour="orange"]')?.className).toContain('border-orange-500')
  expect(container.querySelector('[data-card-colour="violet"]')?.className).toContain('bg-violet-500')
  const squares = screen.getByTestId('session-row-card-squares')
  expect(squares.getAttribute('aria-label')).toBe('Stuck 2 · Done, not yet seen 1')
  act(() => {
    fireEvent.pointerMove(squares, { pointerType: 'mouse' })
    vi.advanceTimersByTime(700)
  })
  expect(screen.getByRole('tooltip').textContent).toBe(squares.getAttribute('aria-label'))
})

it('renders nothing without card fields or unknown execution', () => {
  draw()
  expect(screen.queryByTestId('session-row-card-squares')).toBeNull()
})

it('uses the same ordered text for the accessible label and top tooltip', () => {
  vi.useFakeTimers()
  draw({ kanban_blocked: 1, kanban_active: 3, kanban_parked: 2 })
  const squares = screen.getByTestId('session-row-card-squares')
  expect(squares.getAttribute('aria-label')).toBe('Needs you 1')
  act(() => {
    fireEvent.pointerMove(squares, { pointerType: 'mouse' })
    vi.advanceTimersByTime(700)
  })
  expect(screen.getByRole('tooltip').textContent).toBe(squares.getAttribute('aria-label'))
})
