/**
 * [daveey] Pure model for the hub-sync plugin: which replica session records are
 * candidates for the offline fallback (the ordinary sidebar applies its own
 * policy to them, see store/offline-sessions.ts) and the owner requests.
 */
import type { HubPending, HubRecord, HubRequestBody } from '@hermes/plugin-sdk'

export interface HubSessionRow extends HubRecord {
  id: string
  profile?: string
  title?: null | string
  source?: null | string
  activity?: null | number
  archived?: boolean
  hidden?: boolean
  pinned?: boolean
  running?: boolean
}

/** A live sidebar row reduced to what dedupe needs: its hub gateway + id. */
export interface LiveSessionKey {
  gateway: string
  id: string
}

/**
 * Replica rows worth a sidebar row: other gateways only, never one that is
 * live right now (its live rows are primary), never a (gateway, id) the live
 * list already has, archived/hidden dropped, first copy per (gateway, id).
 */
export function replicaSessionsToShow(
  hub: readonly HubSessionRow[],
  live: readonly LiveSessionKey[],
  isGatewayLive: (gateway: string) => boolean
): HubSessionRow[] {
  const seen = new Set(live.map(entry => `${entry.gateway}\u0000${entry.id}`))
  const out: HubSessionRow[] = []

  for (const record of hub) {
    if (!record?.id || record.local || record.archived || record.hidden || isGatewayLive(record.gateway)) {
      continue
    }

    const key = `${record.gateway}\u0000${record.id}`

    if (seen.has(key)) {
      continue
    }

    seen.add(key)
    out.push(record)
  }

  return out
}

export type HubSessionAction = 'archive' | 'pin' | 'rename' | 'unpin'

/** The owner request for an action on a replica row (optimistic-concurrency
 *  checked against the record version the row was read at). */
export function sessionRequestBody(
  row: HubSessionRow,
  action: HubSessionAction,
  args?: Record<string, unknown>
): HubRequestBody {
  return {
    action,
    ...(args ? { args } : {}),
    base_version: row.version,
    coll: 'sessions',
    gateway: row.gateway,
    key: row.key
  }
}

/** Copy of `rows` with `target` carrying a pending marker (immutable). */
export function markRowPending<T extends HubRecord>(rows: readonly T[], target: T, pending: HubPending): T[] {
  return rows.map(record =>
    record.gateway === target.gateway && record.key === target.key
      ? { ...record, pending: [...(record.pending ?? []), pending] }
      : record
  )
}

export function isRowPending(record: Pick<HubRecord, 'pending'>): boolean {
  return Boolean(record.pending?.some(entry => entry.state === 'local' || entry.state === 'queued'))
}
