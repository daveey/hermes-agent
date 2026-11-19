import { atom } from 'nanostores'

import { setSessionMutedRemote } from '@/hermes'
import { translateNow } from '@/i18n'
import { isMissingRestEndpoint } from '@/lib/gateway-rpc'
import { Codecs, persistentAtom } from '@/lib/persisted'
import type { SessionInfo } from '@/types/hermes'

import { notifyError } from './notifications'
import { $sessions, lineageAliases, setSessions } from './session'
import { type MuteRow, parkRows } from './session-park-rows'

export { type MuteRow, resetParkRowsForTests } from './session-park-rows'

/**
 * [daveey] Sessions the user marked "blocked" with ⌘B — parked on something
 * outside the chat (a review, a person, a long job). The row's status stripe
 * turns grey and ⌘] / ⌘[ skip it. A manual flag: only ⌘B (again) clears it.
 *
 * Local to this window's storage. Compression rotates a conversation's stored
 * id, so membership is checked across the whole lineage.
 *
 * [daveey] session-outcome: the backend owns the park now (`sessions.muted`,
 * `POST /api/sessions/{id}/mute`, read back as `row.muted`). This list stays as
 * the optimistic cache, and as the only record against a backend that predates
 * `muted` (the row carries no `muted`, or the endpoint 404s): then ⌘B behaves
 * exactly as before.
 */
export const $blockedSessionIds = persistentAtom<string[]>('hermes.desktop.blockedSessionIds', [], Codecs.stringArray)

/** [daveey] How long a ⌘B write outranks the row's `muted`: a list page already in flight when we POST can land
 *  after the ack carrying the old value (same pattern as the unread write guard). */
export const MUTE_WRITE_GUARD_MS = 10_000
export const MUTE_ACK_HOLD_MS = 10 * 60_000

export interface MuteWrite {
  acked?: boolean
  at: number
  value: boolean
}

/** [daveey] id → the park value we last wrote and when. Fresh entries outrank `row.muted`. */
export const $muteWriteGuard = atom<ReadonlyMap<string, MuteWrite>>(new Map())

type LineageRows = Parameters<typeof lineageAliases>[1]

/** Remember drawn rows outside the recent list without retaining an unbounded history. */
export function rememberParkRow(row: MuteRow): void {
  parkRows.delete(row.id)
  parkRows.set(row.id, row)

  if (parkRows.size > 1000) {
    parkRows.delete(parkRows.keys().next().value!)
  }

  const aliases = lineageAliases(row.id, [...parkRows.values(), ...$sessions.get()])

  for (const alias of aliases) {
    const write = $muteWriteGuard.get().get(alias)

    if (write?.acked && (row.muted === write.value || Date.now() - write.at >= MUTE_ACK_HOLD_MS)) {
      setMuteWrite(alias, null)
    }
  }
}

export function parkRowFor(id: string): MuteRow | undefined {
  const rows = [...parkRows.values(), ...$sessions.get()]

  for (const alias of lineageAliases(id, rows)) {
    const row = parkRows.get(alias)

    if (row) {
      return row
    }
  }

  return undefined
}

export function isSessionBlocked(
  id: string,
  blocked: readonly string[] = $blockedSessionIds.get(),
  sessions: LineageRows = $sessions.get()
): boolean {
  if (blocked.length === 0) {
    return false
  }

  return lineageAliases(id, sessions).some(alias => blocked.includes(alias))
}

/** [daveey] The park a row shows: a fresh ⌘B write, else the backend's `row.muted`, else the local list (an older
 *  backend that stamps no `muted`). Pure, so every surface resolves it the same way. */
export function resolveSessionParked(
  localBlocked: boolean,
  row: null | Partial<Pick<SessionInfo, 'muted'>> | undefined,
  write: MuteWrite | undefined,
  now: number = Date.now()
): boolean {
  if (write && now - write.at < (write.acked ? MUTE_ACK_HOLD_MS : MUTE_WRITE_GUARD_MS)) {
    return write.value
  }

  if (typeof row?.muted === 'boolean') {
    return row.muted
  }

  return localBlocked
}

/** [daveey] The ⌘B write recorded for any alias of `id`, if any. */
export function muteWriteFor(
  id: string,
  guard: ReadonlyMap<string, MuteWrite> = $muteWriteGuard.get(),
  sessions: LineageRows = $sessions.get()
): MuteWrite | undefined {
  if (guard.size === 0) {
    return undefined
  }

  for (const alias of lineageAliases(id, sessions)) {
    const write = guard.get(alias)

    if (write) {
      return write
    }
  }

  const remembered = parkRowFor(id)

  return remembered ? guard.get(remembered.id) : undefined
}

/** [daveey] Whether `id` is parked, reading the stores. Pass `row` when the caller holds it (a project-tree row is
 *  not in `$sessions`). */
export function isSessionParked(id: string, row?: null | Partial<Pick<SessionInfo, 'muted'>>): boolean {
  const sessions = $sessions.get()
  const listed = row ?? sessions.find(entry => entry.id === id)

  return resolveSessionParked(isSessionBlocked(id, $blockedSessionIds.get(), sessions), listed, muteWriteFor(id))
}

function setLocalBlocked(id: string, blocked: boolean, sessions: LineageRows): void {
  const current = $blockedSessionIds.get()

  if (blocked) {
    if (!isSessionBlocked(id, current, sessions)) {
      $blockedSessionIds.set([...current, id])
    }

    return
  }

  const aliases = new Set(lineageAliases(id, sessions))
  $blockedSessionIds.set(current.filter(entry => !aliases.has(entry)))
}

function setMuteWrite(id: string, write: MuteWrite | null): void {
  const next = new Map($muteWriteGuard.get())

  if (write) {
    next.set(id, write)
  } else {
    next.delete(id)
  }

  $muteWriteGuard.set(next)
}

/** [daveey] The row after a park write, until the next list page brings the backend's own outcome. Muting stamps a
 *  `muted` outcome (what the backend refreshes to); unmuting drops a `muted` one, so the stripe falls back to the
 *  derived logic instead of staying grey on the stale kind. Any other outcome is left as it is. */
export function withRowMuted(row: SessionInfo, muted: boolean, now: number = Date.now()): SessionInfo {
  if (muted) {
    return { ...row, muted, outcome: { kind: 'muted', source: 'derived', at: now / 1000 } }
  }

  if (row.outcome?.kind === 'muted') {
    const { outcome: _stale, ...rest } = row

    return { ...rest, muted }
  }

  return { ...row, muted }
}

function setRowMuted(id: string, muted: boolean): void {
  setSessions(rows =>
    rows.some(row => row.id === id) ? rows.map(row => (row.id === id ? withRowMuted(row, muted) : row)) : rows
  )
}

/** [daveey] Write a park through to the backend. Optimistic: the local list and the guard paint at once; a failed
 *  POST rolls both back and shows the normal error toast. A backend without the endpoint keeps the local-only
 *  park, as before. */
export async function writeSessionMuted(
  row: MuteRow,
  muted: boolean,
  post: (row: MuteRow, muted: boolean) => Promise<unknown> = setSessionMutedRemote
): Promise<void> {
  const sessions = $sessions.get()
  const previous = isSessionParked(row.id, row)

  setMuteWrite(row.id, { at: Date.now(), value: muted })
  setLocalBlocked(row.id, muted, sessions)

  try {
    await post(row, muted)
    setMuteWrite(row.id, { at: Date.now(), value: muted, acked: true })
    setRowMuted(row.id, muted)
  } catch (error) {
    if (isMissingRestEndpoint(error)) {
      // An older backend: the local list is the only record, exactly as before this endpoint existed.
      setMuteWrite(row.id, null)

      return
    }

    setMuteWrite(row.id, null)
    setLocalBlocked(row.id, previous, $sessions.get())
    notifyError(error, translateNow('sidebar.row.muteFailed'))
  }
}

/** [daveey] Profiles (per gateway) whose old local ⌘B parks were pushed to the backend once. */
export const $mutedMigrationDone = persistentAtom<string[]>(
  'hermes.desktop.mutedMigrationDone.v1',
  [],
  Codecs.stringArray
)

const migrationScope = (row: Pick<SessionInfo, 'connection_id' | 'profile'>) =>
  `${row.connection_id ?? 'primary'}\u0000${row.profile || 'default'}`

const migrating = new Set<string>()

/** [daveey] One-time migration of the pre-backend ⌘B list. The first list page whose rows carry `muted` (a backend
 *  that owns the park) POSTs `/mute {muted: true}` for every locally parked row it reports `muted=false`, then
 *  records the profile so it never runs again. A 404 leaves the local list alone (an older backend, or a row the
 *  backend no longer has) and still counts as done; any other failure leaves the profile unmarked, so the next
 *  load retries. Rows not on that page are not migrated. */
export async function migrateLocalParks(
  rows: readonly SessionInfo[] = [],
  post: (row: MuteRow, muted: boolean) => Promise<unknown> = setSessionMutedRemote
): Promise<void> {
  const done = new Set($mutedMigrationDone.get())
  const byScope = new Map<string, SessionInfo[]>()

  for (const row of rows) {
    if (typeof row.muted !== 'boolean') {
      continue
    }

    const scope = migrationScope(row)

    if (done.has(scope) || migrating.has(scope)) {
      continue
    }

    byScope.set(scope, [...(byScope.get(scope) ?? []), row])
  }

  await Promise.all(
    [...byScope].map(async ([scope, scoped]) => {
      migrating.add(scope)

      try {
        const blocked = $blockedSessionIds.get()
        const targets = scoped.filter(row => row.muted === false && isSessionBlocked(row.id, blocked, $sessions.get()))
        let retry = false

        for (const row of targets) {
          try {
            await post(row, true)
            setMuteWrite(row.id, { at: Date.now(), value: true })
            setRowMuted(row.id, true)
          } catch (error) {
            if (!isMissingRestEndpoint(error)) {
              retry = true
            }
          }
        }

        if (!retry) {
          $mutedMigrationDone.set([...new Set([...$mutedMigrationDone.get(), scope])])
        }
      } finally {
        migrating.delete(scope)
      }
    })
  )
}

/** Flip the flag; returns the new state. Unblocking clears every alias. [daveey] A persisted row writes through to
 *  the backend (`writeSessionMuted`); a draft with no row stays local-only. */
export function toggleSessionBlocked(id: string): boolean {
  const sessions = $sessions.get()
  const row = sessions.find(entry => entry.id === id) ?? parkRowFor(id)
  const next = !isSessionParked(id, row)

  if (!row) {
    setLocalBlocked(id, next, sessions)

    return next
  }

  void writeSessionMuted(row, next)

  return next
}
