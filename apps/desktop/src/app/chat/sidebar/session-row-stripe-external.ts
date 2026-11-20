import { formatSessionExternalHover, kanbanExternalStripeCounts } from '@/lib/external-run'
import type { SessionStatusStripe } from '@/store/session-dot-state'
import type { SessionInfo } from '@/types/hermes'

/** [daveey] Hollow cloud when every external card is unknown (stamps or client-derived). */
export function unknownExternalCloud(
  session: SessionInfo,
  statusStripe: SessionStatusStripe,
  now = Date.now() / 1000
): boolean {
  if (statusStripe !== 'external') {
    return false
  }

  const counts = kanbanExternalStripeCounts(session, now)

  return counts.external > 0 && counts.unknown >= counts.external
}

const stampedExternalTotal = (session: SessionInfo): number =>
  (session.kanban_external ?? 0) + (session.kanban_external_down ?? 0)

/** Hover copy for an external/external_down stripe; absent cards → null. */
export function sessionExternalStripeTip(
  session: SessionInfo,
  statusStripe: SessionStatusStripe,
  now = Date.now() / 1000
): string | null {
  if (statusStripe !== 'external' && statusStripe !== 'external_down') {
    return null
  }

  return formatSessionExternalHover(session.kanban_external_cards, now, stampedExternalTotal(session))
}
