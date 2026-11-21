import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { I18nProvider } from '@/i18n'

import { sessionCardColours, SessionRowCardSquares } from './session-row-card-squares'

const session = (fields: Partial<SessionInfo> = {}) => fields as SessionInfo

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
  it('amber combines stalled and attention', () => {
    expect(sessionCardColours(session({ kanban_stalled: 2, kanban_attention: 1 }), 0)).toEqual([
      { colour: 'amber', count: 3 }
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
  it('green counts unseen results', () => {
    expect(sessionCardColours(session({ kanban_unseen: 2 }), 0)).toEqual([{ colour: 'green', count: 2 }])
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
    ).toEqual(['red', 'pink', 'amber', 'blue', 'grey', 'green'])
  })
})

it('renders one 6px square for two cards of the same colour', () => {
  const { container } = draw({ kanban_blocked: 2 })
  expect(container.querySelectorAll('[data-card-colour="red"]')).toHaveLength(1)
  expect(container.querySelector('[data-card-colour="red"]')?.className).toContain('size-[6px]')
  expect(screen.getByRole('img').getAttribute('aria-label')).toBe('Needs you 2')
})

it('renders nothing without card fields or unknown execution', () => {
  draw()
  expect(screen.queryByTestId('session-row-card-squares')).toBeNull()
})

it('uses the same ordered text for the accessible label and top tooltip', () => {
  vi.useFakeTimers()
  draw({ kanban_blocked: 1, kanban_active: 3, kanban_parked: 2 })
  const squares = screen.getByTestId('session-row-card-squares')
  expect(squares.getAttribute('aria-label')).toBe('Needs you 1 · Running 1 · Waiting 2')
  act(() => {
    fireEvent.pointerMove(squares, { pointerType: 'mouse' })
    vi.advanceTimersByTime(700)
  })
  expect(screen.getByRole('tooltip').textContent).toBe(squares.getAttribute('aria-label'))
})
