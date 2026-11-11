/**
 * [daveey] Status-bar task meter: one bar split by where open cards stand.
 * yellow = not ready yet (triage/todo/scheduled), green = ready, red = blocked
 * (waiting on the user), blue = running (review counts as running: an agent is
 * still working it). Segment widths are proportional to the counts; only
 * done/archived cards are left out.
 */

import type { KanbanBoard } from './types'

export type MeterSegment = 'blocked' | 'notReady' | 'ready' | 'running'

export type MeterCounts = Record<MeterSegment, number>

export const METER_ORDER: readonly MeterSegment[] = ['running', 'ready', 'notReady', 'blocked']

export const METER_COLORS: Record<MeterSegment, string> = {
  blocked: '#d64545',
  notReady: '#d4a72c',
  ready: '#2ea043',
  running: '#388bfd'
}

const SEGMENT_OF: Record<string, MeterSegment> = {
  blocked: 'blocked',
  ready: 'ready',
  review: 'running',
  running: 'running',
  scheduled: 'notReady',
  todo: 'notReady',
  triage: 'notReady'
}

export function meterCounts(board: Pick<KanbanBoard, 'columns'>): MeterCounts {
  const counts: MeterCounts = { blocked: 0, notReady: 0, ready: 0, running: 0 }

  for (const col of board.columns) {
    const segment = SEGMENT_OF[col.name]

    if (segment) {
      counts[segment] += col.tasks.length
    }
  }

  return counts
}

export const meterTotal = (c: MeterCounts) => c.blocked + c.notReady + c.ready + c.running

export function TaskMeter({ counts }: { counts: MeterCounts }) {
  const total = meterTotal(counts)

  return (
    <span
      className="inline-flex h-1.5 w-14 overflow-hidden rounded-full bg-(--ui-stroke-secondary)"
      data-slot="kanban-meter"
    >
      {METER_ORDER.map(segment =>
        counts[segment] > 0 ? (
          <span
            data-count={counts[segment]}
            data-segment={segment}
            key={segment}
            style={{ background: METER_COLORS[segment], width: `${(counts[segment] / total) * 100}%` }}
          />
        ) : null
      )}
    </span>
  )
}
