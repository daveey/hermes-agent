import type { SessionInfo, SidebarSessionsResponse } from '@/hermes'

/**
 * [daveey] The sidebar list follows the window's connection. While the window is on another gateway (the hub), that
 * list carries none of this Mac's own chats, so `mergeOfflineFallback` kept painting them from the cold-start boot
 * snapshot: frozen at launch, with no activity stamps (`cron_active`, `kanban_*`, `outcome`, ...). Those copies
 * shadow the stamped projects-tree rows, so a chat that owns a live cron job (or waits on cards) lost its stripe
 * for as long as the window stayed on the hub.
 *
 * Fold the primary backend's own recents page (pinned rows included) into the window's page, tagged with the
 * primary's connection id. A failed or unreachable primary read changes nothing: the window's page is returned as
 * is and the fallback rows keep painting as before.
 */
export async function withPrimaryRecents(
  page: Promise<SidebarSessionsResponse>,
  windowConnection: null | string | undefined,
  primaryConnection: string,
  fetchPrimary: () => Promise<SidebarSessionsResponse>
): Promise<SidebarSessionsResponse> {
  const remote = Boolean(windowConnection?.trim()) && windowConnection?.trim() !== primaryConnection

  if (!remote) {
    return page
  }

  const [result, own] = await Promise.all([page, fetchPrimary().catch(() => null)])

  return mergePrimaryRecents(result, own, primaryConnection)
}

/** Exported for tests: the window's page plus the primary's recents rows it does not already carry. */
export function mergePrimaryRecents(
  result: SidebarSessionsResponse,
  own: null | SidebarSessionsResponse | undefined,
  primaryConnection: string
): SidebarSessionsResponse {
  const ownRows = own?.recents?.sessions

  if (!own || own.recents?.failed === true || own.recents?.retry === true || !ownRows?.length) {
    return result
  }

  const key = (row: SessionInfo) =>
    `${row.connection_id?.trim() || primaryConnection}\u0000${(row.profile ?? '').trim() || 'default'}\u0000${row.id}`

  const windowRows = result.recents?.sessions ?? []
  const have = new Set(windowRows.map(key))

  const added = ownRows
    .map(row => (row.connection_id?.trim() ? row : { ...row, connection_id: primaryConnection }))
    .filter(row => !have.has(key(row)))

  if (!added.length) {
    return result
  }

  return { ...result, recents: { ...result.recents, sessions: [...windowRows, ...added] } }
}
