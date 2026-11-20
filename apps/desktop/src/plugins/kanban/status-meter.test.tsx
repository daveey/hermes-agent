import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { en } from './i18n'
import { METER_COLORS, meterCounts, meterTotal, TaskMeter } from './status-meter'

const task = (id: string) => ({ id, status: 'x', title: id })
const col = (name: string, n: number) => ({ name, tasks: Array.from({ length: n }, (_, i) => task(`${name}-${i}`)) })

afterEach(cleanup)

describe('kanban status meter', () => {
  it('maps lanes to segments: triage/todo/scheduled are not ready, review runs; done/archived are left out', () => {
    const counts = meterCounts({
      columns: [
        col('triage', 1),
        col('todo', 2),
        col('scheduled', 3),
        col('ready', 4),
        col('running', 5),
        col('blocked', 6),
        col('review', 7),
        col('done', 8),
        col('archived', 9)
      ]
    })

    expect(counts).toEqual({ blocked: 6, external: 0, externalDown: 0, notReady: 6, ready: 4, running: 12 })
    expect(meterTotal(counts)).toBe(28)
  })

  it('a review-only board still counts (the pill must not vanish), and running -> review keeps the total', () => {
    expect(meterTotal(meterCounts({ columns: [col('review', 1)] }))).toBe(1)
    const before = meterCounts({ columns: [col('running', 2), col('ready', 1)] })
    const after = meterCounts({ columns: [col('running', 1), col('review', 1), col('ready', 1)] })
    expect(meterTotal(after)).toBe(meterTotal(before))
    expect(after.running).toBe(before.running)
  })

  it('draws one coloured segment per non-empty state, widths proportional to the counts', () => {
    const { container } = render(
      <TaskMeter counts={{ blocked: 1, external: 0, externalDown: 0, notReady: 1, ready: 0, running: 2 }} />
    )

    const segs = [...container.querySelectorAll<HTMLElement>('[data-segment]')]

    expect(segs.map(s => s.dataset.segment)).toEqual(['running', 'notReady', 'blocked'])
    expect(segs.map(s => s.style.width)).toEqual(['50%', '25%', '25%'])
    // jsdom normalises hex to rgb
    expect(segs[0].style.background).toBe('rgb(56, 139, 253)')
    expect(METER_COLORS).toEqual({
      blocked: '#d64545',
      external: '#38bdf8',
      externalDown: '#ef4444',
      notReady: '#d4a72c',
      ready: '#2ea043',
      running: '#388bfd'
    })
  })

  it('tooltip names every segment', () => {
    expect(en.countTip({ blocked: 4, external: 0, externalDown: 0, notReady: 3, ready: 2, running: 1 })).toBe(
      'Kanban — 1 running · 2 ready · 3 not ready · 4 blocked on you · 0 external · 0 external down'
    )
  })

  it('scheduled + external is sky; external_down is red, not notReady', () => {
    const running = {
      id: 'e1',
      status: 'scheduled',
      title: 'CI',
      external: {
        adapter: 'github_run',
        health: 'ok',
        job: 'running',
        next_due_at: 2000,
        observed_at: 1000,
        service: 'GitHub Actions'
      }
    }

    const down = {
      id: 'e2',
      status: 'scheduled',
      title: 'CI down',
      external: { health: 'unreachable', job: 'unknown', service: 'GitHub Actions' }
    }

    const counts = meterCounts({ columns: [{ name: 'scheduled', tasks: [running, down] }] }, 1000)

    expect(counts.external).toBe(1)
    expect(counts.externalDown).toBe(1)
    expect(en.countTip(counts)).toContain('1 external ·')
    expect(en.countTip(counts)).toContain('1 external down')
    expect(counts.notReady).toBe(0)
    expect(METER_COLORS.external).toBe('#38bdf8')
    expect(METER_COLORS.externalDown).toBe('#ef4444')
  })
})
