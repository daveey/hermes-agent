import type { SessionInfo } from '@/hermes'
import { deriveExternalVisual } from '@/lib/external-run'

export const DEPLOY_COLOUR = '#a371f7'

/** Category evidence survives an unknown watch; a down producer must retain its outage colour. */
export function deployCardCount(session: SessionInfo, now = Date.now() / 1000): number {
  const ids = new Set(
    (session.kanban_external_cards ?? [])
      .filter(
        card =>
          (card.eta?.wait_kind === 'deploy' || card.external?.adapter === 'deploy') &&
          deriveExternalVisual(card.external, now) !== 'down'
      )
      .map(card => card.task_id)
  )

  return ids.size
}

export function allDeployWaits(session: SessionInfo): boolean {
  const count = deployCardCount(session)

  return count > 0 && count === session.kanban_active && !session.kanban_external_down
}
