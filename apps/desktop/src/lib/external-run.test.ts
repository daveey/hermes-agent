import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  deriveExternalVisual,
  type ExternalCard,
  type ExternalInfo,
  formatExternalHover,
  formatSessionExternalHover,
  kanbanExternalStripeCounts
} from './external-run'

const gh = (over: Partial<ExternalInfo> = {}): ExternalInfo => ({
  adapter: 'github_run',
  health: 'ok',
  job: 'running',
  next_due_at: 1_000 + 300,
  observed_at: 1_000,
  reason: "job 'tests', 12 min",
  service: 'GitHub Actions',
  url: 'https://github.com/o/r/actions/runs/1',
  ...over
})

describe('deriveExternalVisual (next_due_at staleness)', () => {
  it('absent external → no claim', () => {
    expect(deriveExternalVisual(undefined, 1_000)).toBeNull()
    expect(deriveExternalVisual(null, 1_000)).toBeNull()
  })

  it('fake-timer: blue → unknown → red from next_due_at (interval = due - observed)', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_000 * 1000)
    const ext = gh()

    expect(deriveExternalVisual(ext, Date.now() / 1000)).toBe('running')

    vi.setSystemTime((1_000 + 301) * 1000)
    expect(deriveExternalVisual(ext, Date.now() / 1000)).toBe('unknown')

    vi.setSystemTime((1_000 + 300 + 301) * 1000)
    expect(deriveExternalVisual(ext, Date.now() / 1000)).toBe('down')
  })

  it('auth_failed / unreachable is down immediately, even before next_due_at', () => {
    expect(deriveExternalVisual(gh({ health: 'auth_failed', job: 'running' }), 1_000)).toBe('down')
    expect(deriveExternalVisual(gh({ health: 'unreachable', job: 'queued' }), 1_000)).toBe('down')
  })

  it('rate_limited stays unknown and never counts as down from health', () => {
    expect(deriveExternalVisual(gh({ health: 'rate_limited', job: 'running' }), 1_000)).toBe('unknown')
  })
})

describe('formatExternalHover (interface-hover.md)', () => {
  const now = 1_000 + 60

  it('running', () => {
    const hover = formatExternalHover(gh(), now)

    expect(hover?.line1).toBe('Running on GitHub Actions — in progress')
    expect(hover?.line2).toContain("job 'tests', 12 min")
    expect(hover?.line2).toContain('last checked 1 min ago')
    expect(hover?.line2).toContain('next in 4 min')
    expect(hover?.line2).toContain('https://github.com/o/r/actions/runs/1')
  })

  it('queued', () => {
    const hover = formatExternalHover(gh({ job: 'queued', reason: '' }), now)

    expect(hover?.line1).toBe('Waiting on GitHub Actions — queued, no runner yet')
  })

  it('unknown', () => {
    const ext = gh({ health: 'ok', job: 'unknown', observed_at: now - 7 * 60, next_due_at: now + 3 * 60 })
    const hover = formatExternalHover(ext, now, { visual: 'unknown' })

    expect(hover?.line1).toBe('Status unknown on GitHub Actions — last check 7 min ago')
    expect(hover?.line2).toContain('next check in 3 min')
  })

  it('down', () => {
    const hover = formatExternalHover(gh({ health: 'unreachable', reason: '2 failed checks (502)' }), now, {
      taskId: 't_x',
      visual: 'down'
    })

    expect(hover?.line1).toBe('GitHub Actions unreachable — 2 failed checks (502)')
    expect(hover?.line2).toContain('Agent triaging (t_x)')
  })

  it('stale-derived down does not claim health ok or Agent triaging', () => {
    const ext = gh()
    const stale = 1_000 + 601

    expect(deriveExternalVisual(ext, stale)).toBe('down')

    const hover = formatExternalHover(ext, stale, { taskId: 't_x' })

    expect(hover?.line1.toLowerCase()).not.toContain(' ok')
    expect(hover?.text).not.toContain('Agent triaging')
    expect(hover?.text).not.toContain("job 'tests'")
    expect(hover?.line1).toMatch(/overdue|no recent check/i)
  })

  it.each([
    [1420, 'unknown', '2 min'],
    [1901, 'down', '10 min']
  ])('a past-due check at %s is overdue, not future', (now, visual, late) => {
    const hover = formatExternalHover(gh(), now as number)

    expect(deriveExternalVisual(gh(), now as number)).toBe(visual)
    expect(hover?.line2).toContain(`check overdue by ${late}`)
    expect(hover?.line2).not.toContain('next check in')
  })

  it('a finished job with health ok is not unknown sky', () => {
    expect(deriveExternalVisual(gh({ job: 'succeeded' }), 1_000)).toBeNull()
    expect(formatExternalHover(gh({ job: 'failed' }), 1_000)).toBeNull()
  })

  it('missing-field fallback: no tooltip claim', () => {
    expect(formatExternalHover(undefined, now)).toBeNull()
    expect(formatExternalHover(null, now)).toBeNull()
  })
})

describe('formatSessionExternalHover (sidebar, max 5 + N more)', () => {
  it('lists each card then +N more', () => {
    const cards: ExternalCard[] = Array.from({ length: 7 }, (_, i) => ({
      external: gh({ reason: `job '${i}'` }),
      task_id: `t_${i}`,
      title: `Card ${i}`
    }))

    const text = formatSessionExternalHover(cards, 1_060)

    expect(text).toContain('Card 0')
    expect(text).toContain('Card 4')
    expect(text).not.toContain('Card 5')
    expect(text).toMatch(/\+2 more/)
  })

  it('+N more uses stamped totals when the cards list is capped', () => {
    const cards: ExternalCard[] = Array.from({ length: 10 }, (_, i) => ({
      external: gh({ reason: `job '${i}'` }),
      task_id: `t_${i}`,
      title: `Card ${i}`
    }))

    const text = formatSessionExternalHover(cards, 1_060, 15)

    expect(text).toMatch(/\+10 more/)
    expect(text).not.toMatch(/\+5 more/)
  })

  it('missing cards → no tooltip', () => {
    expect(formatSessionExternalHover(undefined, 1_000)).toBeNull()
    expect(formatSessionExternalHover([], 1_000)).toBeNull()
  })
})

describe('kanbanExternalStripeCounts', () => {
  it('absent fields → zeros (today)', () => {
    expect(kanbanExternalStripeCounts({}, 1_000)).toEqual({ down: 0, external: 0, unknown: 0 })
  })

  it('trusts count stamps when no cards', () => {
    expect(
      kanbanExternalStripeCounts({ kanban_external: 2, kanban_external_unknown: 1, kanban_external_down: 3 }, 1_000)
    ).toEqual({ down: 3, external: 2, unknown: 1 })
  })

  it('re-derives from cards + next_due_at', () => {
    const cards: ExternalCard[] = [
      { external: gh(), task_id: 't_run', title: 'run' },
      { external: gh({ job: 'queued' }), task_id: 't_q', title: 'q' }
    ]

    expect(kanbanExternalStripeCounts({ kanban_external_cards: cards }, 1_000)).toEqual({
      down: 0,
      external: 2,
      unknown: 0
    })
    expect(kanbanExternalStripeCounts({ kanban_external_cards: cards }, 1_000 + 301)).toEqual({
      down: 0,
      external: 2,
      unknown: 2
    })
    expect(kanbanExternalStripeCounts({ kanban_external_cards: cards }, 1_000 + 601)).toEqual({
      down: 2,
      external: 0,
      unknown: 0
    })
  })

  it('takes the max of derived cards and stamped counts so a capped list cannot drop red', () => {
    expect(
      kanbanExternalStripeCounts(
        { kanban_external: 3, kanban_external_cards: [{ task_id: 'a' }], kanban_external_down: 2 },
        1_000
      )
    ).toEqual({ down: 2, external: 3, unknown: 0 })

    const ten: ExternalCard[] = Array.from({ length: 10 }, (_, i) => ({
      external: gh(),
      task_id: `t_${i}`
    }))

    expect(
      kanbanExternalStripeCounts({ kanban_external: 10, kanban_external_cards: ten, kanban_external_down: 2 }, 1_000)
        .down
    ).toBe(2)
  })
})

afterEach(() => {
  vi.useRealTimers()
})
