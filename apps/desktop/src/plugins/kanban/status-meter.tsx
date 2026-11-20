/**
 * [daveey] Status-bar task meter: one bar split by where open cards stand.
 * yellow = not ready yet (triage/todo/scheduled), green = ready, red = blocked
 * (waiting on the user), blue = running (review counts as running: an agent is
 * still working it). Segment widths are proportional to the counts; only
 * done/archived cards are left out.
 */

import { deriveExternalVisual, type ExternalInfo } from './external-run'
import type { KanbanBoard } from './types'

export type MeterSegment = 'blocked' | 'external' | 'externalDown' | 'notReady' | 'ready' | 'running'

export type MeterCounts = Record<MeterSegment, number>

export const METER_ORDER: readonly MeterSegment[] = ['running', 'external', 'ready', 'notReady', 'blocked', 'externalDown']

export const METER_COLORS: Record<MeterSegment, string> = {
  blocked: '#d64545',
  external: '#38bdf8',
  externalDown: '#ef4444',
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

export function meterCounts(board: Pick<KanbanBoard, 'columns'>, now: number = Date.now() / 1000): MeterCounts {
  const counts: MeterCounts = { blocked: 0, external: 0, externalDown: 0, notReady: 0, ready: 0, running: 0 }

  for (const col of board.columns) {
    if (col.name === 'scheduled') {
      for (const task of col.tasks) {
        const visual = deriveExternalVisual(task.external as ExternalInfo | null | undefined, now)

        if (visual === 'down') {
          counts.externalDown += 1
        } else if (visual === 'running' || visual === 'unknown') {
          counts.external += 1
        } else {
          counts.notReady += 1
        }
      }

      continue
    }

    const segment = SEGMENT_OF[col.name]

    if (segment) {
      counts[segment] += col.tasks.length
    }
  }

  return counts
}

export const meterTotal = (c: MeterCounts) =>
  c.blocked + c.external + c.externalDown + c.notReady + c.ready + c.running

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
