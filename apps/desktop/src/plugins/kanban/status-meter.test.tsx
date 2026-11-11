import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { en } from './i18n'
import { METER_COLORS, meterCounts, meterTotal, TaskMeter } from './status-meter'

const task = (id: string) => ({ id, status: 'x', title: id })
const col = (name: string, n: number) => ({ name, tasks: Array.from({ length: n }, (_, i) => task(`${name}-${i}`)) })

afterEach(cleanup)

describe('kanban status meter', () => {
  it('maps lanes to segments: triage/todo/scheduled are not ready; review/done/archived are left out', () => {
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

    expect(counts).toEqual({ blocked: 6, notReady: 6, ready: 4, running: 5 })
    expect(meterTotal(counts)).toBe(21)
  })

  it('draws one coloured segment per non-empty state, widths proportional to the counts', () => {
    const { container } = render(<TaskMeter counts={{ blocked: 1, notReady: 1, ready: 0, running: 2 }} />)
    const segs = [...container.querySelectorAll<HTMLElement>('[data-segment]')]

    expect(segs.map(s => s.dataset.segment)).toEqual(['running', 'notReady', 'blocked'])
    expect(segs.map(s => s.style.width)).toEqual(['50%', '25%', '25%'])
    // jsdom normalises hex to rgb
    expect(segs[0].style.background).toBe('rgb(56, 139, 253)')
    expect(METER_COLORS).toEqual({ blocked: '#d64545', notReady: '#d4a72c', ready: '#2ea043', running: '#388bfd' })
  })

  it('tooltip names every segment', () => {
    expect(en.countTip({ blocked: 4, notReady: 3, ready: 2, running: 1 })).toBe(
      'Kanban — 1 running · 2 ready · 3 not ready · 4 blocked on you'
    )
  })
})
