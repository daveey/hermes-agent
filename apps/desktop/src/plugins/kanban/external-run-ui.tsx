/** Badge + tooltip for a scheduled card running on an external service. */

import { Tip, useI18n } from '@hermes/plugin-sdk'

import {
  deriveExternalVisual,
  EXTERNAL_DOWN_RED,
  type ExternalInfo,
  formatExternalHover,
  SKY_400
} from './external-run'
import type { KanbanTask } from './types'
import { useExternalRunClock } from './use-external-run-clock'

export function ExternalRunBadge({
  now: explicitNow,
  task
}: {
  now?: number
  task: Pick<KanbanTask, 'external' | 'id' | 'status'>
}) {
  const { t } = useI18n()
  const nowSeconds = useExternalRunClock(task)
  const now = explicitNow ?? nowSeconds

  if (task.status !== 'scheduled') {
    return null
  }

  const visual = deriveExternalVisual(task.external as ExternalInfo | null | undefined, now)

  if (!visual) {
    return null
  }

  const hover = formatExternalHover(task.external as ExternalInfo | null | undefined, now, { taskId: task.id, visual })
  const down = visual === 'down'
  const label = down ? t.sidebar.row.kanbanExternalDown : t.sidebar.row.kanbanExternalRunning
  const tone = down ? EXTERNAL_DOWN_RED : SKY_400

  return (
    <Tip label={hover ? <span className="whitespace-pre-line">{hover.text}</span> : null}>
      <span
        aria-label={hover?.text}
        className="inline-flex cursor-help items-center gap-1 text-[0.625rem] font-semibold uppercase tracking-wide"
        data-slot="kanban-external-badge"
        data-visual={visual}
        style={{ color: tone }}
      >
        {label}
      </span>
    </Tip>
  )
}
