import type { SessionInfo } from '@/types/hermes'

/** True when a stored/lineage id resolves to this session — it matches either
 *  the live id or the stable lineage root (see sessionPinId). The one place the
 *  "same conversation across compression" test lives. */
export const sessionMatchesStoredId = (
  session: Pick<SessionInfo, '_lineage_ids' | '_lineage_root_id' | 'id'>,
  storedSessionId: string
): boolean =>
  session.id === storedSessionId ||
  session._lineage_root_id === storedSessionId ||
  Boolean(session._lineage_ids?.includes(storedSessionId))

// Alias lookup, memoized per sessions-list reference. `lineageAliases` runs
// per cached session state per status projection per message delta — an
// O(sessions) scan there multiplies out to states × sessions × ~30Hz per busy
// session, which is what made a populated recents list drag every stream. The
// list is replaced wholesale (never mutated), so its reference is the cache key.
type LineageRow = Pick<SessionInfo, '_lineage_ids' | '_lineage_root_id' | 'id'>
const lineageIndexBySessions = new WeakMap<readonly LineageRow[], Map<string, string[]>>()

function lineageIndex(sessions: readonly LineageRow[]): Map<string, string[]> {
  const cached = lineageIndexBySessions.get(sessions)

  if (cached) {
    return cached
  }

  const index = new Map<string, string[]>()

  const add = (key: string, value: string) => {
    const bucket = index.get(key)

    if (!bucket) {
      index.set(key, [value])
    } else if (!bucket.includes(value)) {
      bucket.push(value)
    }
  }

  for (const session of sessions) {
    add(session.id, session.id)

    if (session._lineage_root_id) {
      add(session.id, session._lineage_root_id)
      add(session._lineage_root_id, session.id)
      add(session._lineage_root_id, session._lineage_root_id)
    }

    // Chains three+ segments deep: the projected row carries every id the
    // conversation has answered to, so a surface keyed to a MIDDLE segment
    // (it was the tip when the surface opened) still aliases to the rest.
    // Without this, only tip↔root connect and such a surface reads as a
    // different conversation — one chat open twice after a compaction.
    const ids = session._lineage_ids

    if (ids && ids.length > 1) {
      for (const a of ids) {
        for (const b of ids) {
          add(a, b)
        }
      }
    }
  }

  lineageIndexBySessions.set(sessions, index)

  return index
}

/** Every id one conversation answers to: the id we were handed, plus the live
 *  id and lineage root of each session it resolves to.
 *
 *  Status sets are published under a session's CURRENT stored id, but a sidebar
 *  row, a persisted tile, and the route can each hold a different tip of the
 *  same lineage after a compression. Publishing every alias lets those surfaces
 *  keep using a plain membership test instead of each re-deriving lineage —
 *  and getting it wrong, which reads as a running session going idle mid-turn. */
export function lineageAliases(storedId: string, sessions: readonly LineageRow[]): string[] {
  // Every key is in its own bucket by construction, so the bucket IS the
  // alias set. Copied so no caller can mutate the shared index.
  return lineageIndex(sessions).get(storedId)?.slice() ?? [storedId]
}

/** True when two ids name the same conversation across compression tip rotation. */
export function idsShareLineage(
  a: string,
  b: string,
  sessions: readonly Pick<SessionInfo, '_lineage_root_id' | 'id'>[]
): boolean {
  if (a === b) {
    return true
  }

  return sessions.some(session => sessionMatchesStoredId(session, a) && sessionMatchesStoredId(session, b))
}
