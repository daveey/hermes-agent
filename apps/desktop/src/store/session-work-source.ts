import { atom } from 'nanostores'

export interface WorkSourceRead {
  connectionId: string
  profile: string
  storedSessionId: string
  source: 'lineage' | 'process' | 'subagent' | 'tasks'
  available: boolean
}

/** Negative evidence from a real source read. Never writes a task, outcome or completion receipt.
 * A failure remains uncertain until that same qualified source succeeds; changing focus or ending
 * a turn cannot erase it. Successful reads only clear this veto, never establish completion. */
export const $unavailableWorkSources = atom<Readonly<Record<string, true>>>({})

const key = (read: Omit<WorkSourceRead, 'available'>): string =>
  JSON.stringify([read.connectionId, read.profile, read.storedSessionId, read.source])

export function reportWorkSourceRead(read: WorkSourceRead): void {
  if (!read.connectionId || !read.profile || !read.storedSessionId) { return }
  const k = key(read)
  const previous = $unavailableWorkSources.get()

  if (Boolean(previous[k]) === !read.available) { return }
  const next = { ...previous }

  if (read.available) { delete next[k] } else { next[k] = true }
  $unavailableWorkSources.set(next)
}

export function workSourceUnavailable(
  connectionId: string | undefined,
  profile: string | undefined,
  ids: readonly string[],
  sources: Readonly<Record<string, true>> = $unavailableWorkSources.get()
): boolean {
  if (!connectionId || !profile) { return false }

  return ids.some(storedSessionId => (['lineage', 'process', 'subagent', 'tasks'] as const).some(source =>
    sources[key({ connectionId, profile, source, storedSessionId })] === true))
}
