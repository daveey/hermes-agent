import { hermesApiAs } from '@/api/client'
import { isGatewayDrainError } from '@/api/gateway-drain'
import type { SessionInfo } from '@/hermes'
import { sessionMatchesStoredId } from '@/store/session'

/** Read each captured connection directly, not the sidebar aggregate: that
 * aggregate omits dead connections and backfills pins outside its cursor. */
export async function listArchiveOwner(
  id: string,
  connections: readonly string[],
  run: <T>(read: () => Promise<T>) => Promise<T>,
  unresolved: Error
): Promise<SessionInfo> {
  const matches = new Map<string, SessionInfo>()

  for (const connectionId of connections) {
    let total: number | undefined

    for (let offset = 0; total === undefined || offset < total; offset += 300) {
      const page = await run(() =>
        hermesApiAs<{
          sessions: SessionInfo[]
          total: number
          offset: number
          errors?: { error?: string }[]
        }>(
          { connectionId, profile: null },
          {
            path: `/api/profiles/sessions?profile=all&archive_owner_lookup=1&limit=300&offset=${offset}&order=created&min_messages=0&archived=exclude`,
            timeoutMs: 10_000
          }
        )
      )

      const refusal = page.errors?.find(item => isGatewayDrainError(item.error))

      if (refusal) {
        throw new Error(refusal.error)
      }

      if (
        !Number.isSafeInteger(page.total) ||
        page.total < 0 ||
        page.offset !== offset ||
        page.errors?.length ||
        (total !== undefined && page.total !== total) ||
        !Array.isArray(page.sessions) ||
        page.sessions.length < Math.min(300, page.total - offset)
      ) {
        throw unresolved
      }

      total = page.total

      for (const row of page.sessions) {
        if (!sessionMatchesStoredId(row, id)) {
          continue
        }
        const pinned = { ...row, connection_id: connectionId }
        matches.set(JSON.stringify([connectionId, row.profile || 'default', row.id]), pinned)
      }

      if (matches.size > 1) {
        throw unresolved
      }
    }
  }

  if (matches.size !== 1) {
    throw unresolved
  }

  return [...matches.values()][0]
}
