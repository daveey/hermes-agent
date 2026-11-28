import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'
import type { SessionInfo } from '@/hermes'
import type { Translations } from '@/i18n'

import { sessionKanbanLabel } from './session-kanban-label'

/** Return null when absent: the parent row uses presence to reserve its metadata slot. */
export function sessionRowKanbanCounts(
  session: SessionInfo,
  executionUnknown: number,
  r: Translations['sidebar']['row']
) {
  const kanbanOpen = session.kanban_active ?? 0
  const kanbanBlocked = session.kanban_blocked ?? 0
  const kanbanAttention = session.kanban_attention ?? 0
  const kanbanStalled = session.kanban_stalled ?? 0

  const kanbanLabel = [executionUnknown > 0 ? r.kanbanUnknown(executionUnknown) : '', sessionKanbanLabel(session, r)]
    .filter(Boolean)
    .join('; ')

  return kanbanOpen > 0 || kanbanBlocked > 0 || kanbanAttention > 0 || executionUnknown > 0 ? (
    <Tip label={kanbanLabel} side="top">
      <span
        aria-label={kanbanLabel}
        className="pointer-events-auto ml-1 inline-flex items-center gap-1 align-[-1px] tabular-nums"
        data-testid="session-row-kanban-counts"
        role="img"
      >
        {kanbanOpen > 0 && (
          <span className="inline-flex items-center gap-px" data-kanban-count="open">
            <Codicon name="project" size="0.625rem" />
            {kanbanOpen}
          </span>
        )}
        {kanbanBlocked > 0 && (
          <span className="inline-flex items-center gap-px text-amber-500" data-kanban-count="blocked">
            <Codicon name="question" size="0.625rem" />
            {kanbanBlocked}
          </span>
        )}
        {kanbanStalled > 0 && (
          <span className="inline-flex items-center gap-px text-amber-500" data-kanban-count="stalled">
            <Codicon name="watch" size="0.625rem" />
            {kanbanStalled}
          </span>
        )}
        {executionUnknown > 0 && (
          <span className="inline-flex items-center gap-px text-muted-foreground" data-kanban-count="unknown">
            <Codicon name="question" size="0.625rem" />
            {executionUnknown}
          </span>
        )}
        {kanbanAttention > 0 && (
          <span className="inline-flex items-center gap-px text-amber-500" data-kanban-count="attention">
            <Codicon name="warning" size="0.625rem" />
            {kanbanAttention}
          </span>
        )}
      </span>
    </Tip>
  ) : null
}
