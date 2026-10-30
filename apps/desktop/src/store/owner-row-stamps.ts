/**
 * [daveey] Activity stamps for awake-owner fallback rows the projects tree doesn't carry.
 *
 * With another gateway active (the hub), this Mac's older chats come into the sidebar as hub-replica rows. A replica
 * record carries no activity stamps (kanban_*, goal/cron/background/turn, outcome, cwd), and the projects tree that
 * `mergeOfflineFallback` swaps in for them holds only a few previews per project. So a parked chat outside the tree
 * painted no waiting/stuck stripe and sat in Home. This store asks the owning backend for those rows' stamps
 * (`GET /api/profiles/sessions/stamps`, annotated like its list rows) and `mergeOfflineFallback` folds them in.
 */
import { atom } from 'nanostores'

import { hermesApi, type SessionInfo } from '@/hermes'

import { $connectionsRegistry } from './connection-registry-state'

export type OwnerRowStamps = Partial<SessionInfo>

/** Stamps by owner identity (`connection\0profile\0id`, same spelling as `mergeOfflineFallback`). */
export const $ownerRowStamps = atom<ReadonlyMap<string, OwnerRowStamps>>(new Map())

const profileOf = (profile: null | string | undefined) => (profile ?? '').trim() || 'default'

export const ownerStampKey = (row: Pick<SessionInfo, 'connection_id' | 'id' | 'profile'>, id = row.id): string =>
  `${row.connection_id?.trim() || $connectionsRegistry.get()?.primary?.trim() || 'local'}\u0000${profileOf(row.profile)}\u0000${id}`

/** A row is re-asked at most this often; its stamps (kanban counts) change without the row changing. */
export const STAMP_FRESH_MS = 30_000
/** An owner whose stamp request failed (older backend: 404, or unreachable) is not asked again for this long. */
export const STAMP_FAILURE_BACKOFF_MS = 120_000
const BATCH = 100

export type StampFetcher = (connectionId: string, profile: string, ids: string[]) => Promise<OwnerRowStamps[]>

const defaultFetcher: StampFetcher = async (connectionId, profile, ids) => {
  const res = await hermesApi<{ sessions?: OwnerRowStamps[] }>({
    connectionId,
    path: `/api/profiles/sessions/stamps?profile=${encodeURIComponent(profile)}&ids=${ids.map(encodeURIComponent).join(',')}`,
    // This Mac's own backend is observational here: an absent one is not a reason to start it.
    ...(connectionId === 'local' ? { passive: true } : {}),
    timeoutMs: 20_000
  })

  return res?.sessions ?? []
}

const askedAt = new Map<string, number>()
const failedAt = new Map<string, number>()
const inFlight = new Set<string>()

/** Rows that need owner stamps: awake replica rows (`offline_synced_at` set, not `offline_cached`). */
export const rowsNeedingOwnerStamps = (rows: readonly SessionInfo[]): SessionInfo[] =>
  rows.filter(row => row.offline_synced_at !== undefined && !row.offline_cached && !!row.connection_id?.trim())

/** Ask each owner (connection + profile) for the stamps of its awake replica rows; throttled per row and owner. */
export async function refreshOwnerRowStamps(
  rows: readonly SessionInfo[],
  fetcher: StampFetcher = defaultFetcher,
  now: number = Date.now()
): Promise<void> {
  const byOwner = new Map<string, { connectionId: string; profile: string; ids: string[] }>()

  for (const row of rowsNeedingOwnerStamps(rows)) {
    const connectionId = row.connection_id!.trim()
    const profile = profileOf(row.profile)
    const owner = `${connectionId}\u0000${profile}`
    const key = ownerStampKey(row)

    if (inFlight.has(owner) || now - (failedAt.get(owner) ?? -Infinity) < STAMP_FAILURE_BACKOFF_MS) {
      continue
    }

    if (now - (askedAt.get(key) ?? -Infinity) < STAMP_FRESH_MS) {
      continue
    }

    const entry = byOwner.get(owner) ?? { connectionId, ids: [], profile }
    entry.ids.push(row.id)
    byOwner.set(owner, entry)
  }

  await Promise.all(
    [...byOwner.entries()].map(async ([owner, { connectionId, ids, profile }]) => {
      inFlight.add(owner)

      try {
        const answered: OwnerRowStamps[] = []

        for (let i = 0; i < ids.length; i += BATCH) {
          answered.push(...(await fetcher(connectionId, profile, ids.slice(i, i + BATCH))))
        }

        for (const id of ids) {
          askedAt.set(ownerStampKey({ connection_id: connectionId, id, profile }), now)
        }

        const next = new Map($ownerRowStamps.get())

        for (const stamps of answered) {
          if (stamps?.id) {
            const { id, profile: _profile, ...rest } = stamps
            next.set(ownerStampKey({ connection_id: connectionId, id, profile }), rest)
          }
        }

        $ownerRowStamps.set(next)
      } catch {
        failedAt.set(owner, now)
      } finally {
        inFlight.delete(owner)
      }
    })
  )
}

export function _resetOwnerRowStampsForTests(): void {
  askedAt.clear()
  failedAt.clear()
  inFlight.clear()
  $ownerRowStamps.set(new Map())
}
