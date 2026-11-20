/** Badge + tooltip for a scheduled card running on an external service. */

import { Tip } from '@hermes/plugin-sdk'

import {
  deriveExternalVisual,
  EXTERNAL_DOWN_RED,
  type ExternalInfo,
  formatExternalHover,
  SKY_400
} from './external-run'
import type { KanbanTask } from './types'

export function externalCardTone(task: Pick<KanbanTask, 'external' | 'status'>, now: number = Date.now() / 1000): string | null {
  const visual = task.status === 'scheduled' ? deriveExternalVisual(task.external as ExternalInfo | null | undefined, now) : null

  if (visual === 'down') {
    return EXTERNAL_DOWN_RED
  }

  if (visual === 'running' || visual === 'unknown') {
    return SKY_400
  }

  return null
}

export function ExternalRunBadge({
  now = Date.now() / 1000,
  task
}: {
  now?: number
  task: Pick<KanbanTask, 'external' | 'id' | 'status'>
}) {
  if (task.status !== 'scheduled') {
    return null
  }

  const visual = deriveExternalVisual(task.external as ExternalInfo | null | undefined, now)

  if (!visual) {
    return null
  }

  const hover = formatExternalHover(task.external as ExternalInfo | null | undefined, now, { taskId: task.id, visual })
  const down = visual === 'down'
  const label = down ? 'external down' : 'running externally'
  const tone = down ? EXTERNAL_DOWN_RED : SKY_400

  return (
    <Tip label={hover ? <span className="whitespace-pre-line">{hover.text}</span> : null}>
      <span
        className="inline-flex cursor-help items-center gap-1 text-[0.625rem] font-semibold uppercase tracking-wide"
        data-slot="kanban-external-badge"
        data-visual={visual}
        style={{ color: tone }}
        title={hover?.text}
      >
        {label}
      </span>
    </Tip>
  )
}
