import { formatSessionExternalHover } from '@/lib/external-run'
import type { SessionStatusStripe } from '@/store/session-dot-state'
import type { SessionInfo } from '@/types/hermes'

/** [daveey] Hollow cloud when every stamped external card is unknown. */
export function unknownExternalCloud(session: SessionInfo, statusStripe: SessionStatusStripe): boolean {
  return (
    statusStripe === 'external' &&
    (session.kanban_external_unknown ?? 0) > 0 &&
    (session.kanban_external_unknown ?? 0) >= (session.kanban_external ?? 0)
  )
}

/** Hover copy for an external/external_down stripe; absent cards → null. */
export function sessionExternalStripeTip(
  session: SessionInfo,
  statusStripe: SessionStatusStripe,
  now = Date.now() / 1000
): string | null {
  if (statusStripe !== 'external' && statusStripe !== 'external_down') {
    return null
  }

  return formatSessionExternalHover(session.kanban_external_cards, now)
}
