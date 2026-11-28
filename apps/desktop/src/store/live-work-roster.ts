import { atom } from 'nanostores'

import {
  executionIdentityKey,
  projectExecutionObservation,
  selectExecutionObservation,
  summarizeExecutionObservations
} from '@/lib/execution-observations'

import { lineageAliases } from './session'

// App-wide answer to "which conversations still have work running outside
// their turn?" from the backend's `subagent.roster` read. The event-fed stores
// ($subagentsBySession, $backgroundStatusBySession) only hear about sessions
// whose tile is mounted, and their `subagent.list` / `process.list` snapshots
// need the session attached to this transport — so a row that is not on
// screen, and every row right after a reload or socket reconnect, read idle
// while children ran. Keyed by STORED session id (the backend reports the
// durable conversation id), one bucket per backend source so a failing source
// never wipes another's rows. Each bucket carries the time its read was SENT,
// so an event-fed entry is authoritative only when it is newer than that read.

export interface LiveWorkCounts {
  background: number
  subagents: number
  execution_observations?: unknown[]
}

export interface LiveWorkBucket {
  failed?: boolean
  at: number
  /** Stored ids of the sidebar rows this source answers for: a row it covers
   *  and does not list has no live work as of `at`. */
  covers: ReadonlySet<string>
  sessions: Record<string, LiveWorkCounts>
}

export type LiveWorkRoster = Record<string, LiveWorkBucket>

export const $liveWorkRoster = atom<LiveWorkRoster>({})

export function setLiveWorkRosterSource(
  source: string,
  entries: readonly unknown[] | null,
  at = Date.now(),
  covers: readonly string[] = []
) {
  const current = $liveWorkRoster.get()

  if (!entries) {
    if (!(source in current)) {
      return
    }

    const { [source]: _dropped, ...rest } = current
    $liveWorkRoster.set(rest)

    return
  }

  const bucket: Record<string, LiveWorkCounts> = {}

  for (const entry of entries) {
    if (!entry || typeof entry !== 'object') {
      continue
    }

    const row = entry as Record<string, unknown>
    const sid = typeof row.session_id === 'string' ? row.session_id : ''

    if (!sid) {
      continue
    }

    bucket[sid] = {
      background: Number(row.background) || 0,
      subagents: Number(row.subagents) || 0,
      ...(Array.isArray(row.execution_observations) ? { execution_observations: row.execution_observations } : {})
    }
  }

  $liveWorkRoster.set({ ...current, [source]: { at, covers: new Set(covers), sessions: bucket } })
}

/** Stored ids (all lineage aliases) the roster says have `kind` running.
 *  `eventFedAt` maps each alias an event-fed entry speaks for to when that
 *  entry last heard from the backend: an entry NEWER than the roster read stays
 *  authoritative (a child that finished after the read was sent), while an older
 *  one (retained history from a pane that stopped listening) never hides work
 *  the roster saw start since. */
export function rosterLiveIds(
  roster: LiveWorkRoster,
  kind: 'background' | 'subagents',
  eventFedAt: ReadonlyMap<string, number>,
  sessions: Parameters<typeof lineageAliases>[1]
): string[] {
  const ids = new Set<string>()

  for (const { at, sessions: bucket, failed } of Object.values(roster)) {
    if (failed) {
      continue
    }

    for (const [storedId, counts] of Object.entries(bucket)) {
      const work = (counts.execution_observations ?? []).filter(record => {
        const identity = record && typeof record === 'object' ? (record as Record<string, unknown>).identity : null

        return (
          identity &&
          typeof identity === 'object' &&
          (identity as Record<string, unknown>).kind === (kind === 'background' ? 'process' : 'subagent')
        )
      })

      if (
        summarizeExecutionObservations(selectWorkObservations(work, Date.now() / 1000), Date.now() / 1000, 'sidebar')
          .counts.running <= 0
      ) {
        continue
      }

      const aliases = lineageAliases(storedId, sessions)

      if (aliases.some(alias => (eventFedAt.get(alias) ?? -Infinity) >= at)) {
        continue
      }

      for (const alias of aliases) {
        ids.add(alias)
      }
    }
  }

  return [...ids]
}

/** True when a source that answers for this conversation read it AFTER `eventAt`
 *  and found no `kind` work: an event-fed "running" entry that old is a
 *  leftover from a pane that stopped listening (its poll pauses while hidden,
 *  and a silent process exit sends no event), so the row must not stay lit
 *  until the user clicks back into it. A conversation no source covers keeps
 *  its event-fed state. */
export function rosterSaysIdle(
  roster: LiveWorkRoster,
  kind: 'background' | 'subagents',
  aliases: readonly string[],
  eventAt: number
): boolean {
  return Object.values(roster).some(
    ({ at, covers, sessions, failed }) =>
      !failed &&
      at > eventAt &&
      aliases.some(alias => covers.has(alias)) &&
      aliases.every(alias => (sessions[alias]?.[kind] ?? 0) <= 0)
  )
}

/** Record `at` for every alias, keeping the freshest stamp per alias. */
export function noteEventFed(eventFedAt: Map<string, number>, aliases: readonly string[], at: number) {
  for (const alias of aliases) {
    eventFedAt.set(alias, Math.max(at, eventFedAt.get(alias) ?? -Infinity))
  }
}

/** True when the source's last read reported any live work. */
export const rosterSourceHasWork = (roster: LiveWorkRoster, source: string) =>
  Object.values(roster[source]?.sessions ?? {}).some(c => c.subagents > 0 || c.background > 0)

const isRecord = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v)

/** Roster counts discover work, never establish liveness. Missing per-kind records stay unknown
 * beside other fresh records. `source` is the row's owner route, not its unqualified session id. */
export function rosterRowEvidence(roster: LiveWorkRoster, source: string, aliases: readonly string[]) {
  const records: unknown[] = []
  let missing = 0

  for (const alias of aliases) {
    const counts = roster[source]?.sessions[alias]

    if (roster[source]?.failed) {
      // A failed read revokes current state, never outstanding asks: keep each cached record as an
      // explicit unknown under its own identity, with its human requests and original clocks.
      const cached = (counts?.execution_observations ?? []).filter(isRecord).map(record => ({
        ...record,
        state: 'unknown',
        last_known_state: record.state,
        unknown_reason: 'source_unavailable'
      }))

      records.push(...cached)

      if (counts || roster[source].covers.has(alias)) {
        const live = (counts?.background ?? 0) + (counts?.subagents ?? 0)
        missing += Math.max(cached.length > 0 ? 0 : 1, live - cached.length)
      }

      continue
    }

    if (!counts) {
      continue
    }
    const observations = counts.execution_observations ?? []
    records.push(...observations)

    for (const [kind, count] of [
      ['process', counts.background],
      ['subagent', counts.subagents]
    ] as const) {
      const keys = new Set(
        observations.flatMap(record => {
          const id = record && typeof record === 'object' ? (record as Record<string, unknown>).identity : null

          return id && typeof id === 'object' && (id as Record<string, unknown>).kind === kind
            ? [JSON.stringify(id)]
            : []
        })
      )

      missing += Math.max(0, count - keys.size)
    }
  }

  return { records, missing }
}

/** A failed read is not authoritative absence. Retain discovery, not a positive verdict. */
export function failLiveWorkRosterSource(source: string, covers: readonly string[] = []) {
  const current = $liveWorkRoster.get()
  const bucket = current[source] ?? { at: 0, covers: new Set(covers), sessions: {} }
  $liveWorkRoster.set({ ...current, [source]: { ...bucket, failed: true } })
}

/** Select duplicate snapshots by exact execution identity using the shared projector. */
export function selectWorkObservations(records: readonly unknown[], now: number): unknown[] {
  const selected: unknown[] = []
  const groups = new Map<string, { identity: unknown; records: unknown[] }>()

  for (const record of records) {
    const identity = record && typeof record === 'object' ? (record as Record<string, unknown>).identity : undefined
    const key = executionIdentityKey(identity)

    if (!key) {
      selected.push(record)

      continue
    }

    const group = groups.get(key) ?? { identity, records: [] }
    // RPC and row projections of the same cached probe have different derived ages.
    // Re-project at one consumer clock before comparing snapshots; real evidence conflicts remain.
    group.records.push(projectExecutionObservation(record, now))
    groups.set(key, group)
  }

  for (const group of groups.values()) {
    selected.push(selectExecutionObservation(group.records, group.identity, now, 'sidebar'))
  }

  return selected
}
