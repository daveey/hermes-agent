import { atom } from 'nanostores'

export type WorkSource = 'lineage' | 'process' | 'subagent' | 'tasks' | 'control'
export interface WorkSourceRead {
  connectionId: string
  profile: string
  storedSessionId: string
  source: WorkSource
  available: boolean
  /** Captured before dispatch, never inferred from the route at settlement. */
  authorityEpoch?: number
  readVersion?: number
}

interface Observation {
  authorityEpoch?: number
  readVersion?: number
  available: boolean
}
export interface WorkSourceAuthority {
  durable: number | null
  runtime: number | null
}
type Observations = Readonly<Record<string, Observation>>
const requiredSources: readonly WorkSource[] = ['lineage', 'process', 'subagent', 'tasks', 'control']

/** Read evidence only: this store never writes outcomes or completion receipts. */
export const $unavailableWorkSources = atom<Observations>({})

const key = (read: Omit<WorkSourceRead, 'available'>): string =>
  JSON.stringify([read.connectionId, read.profile, read.storedSessionId, read.source])

let nextReadVersion = 0

export function beginWorkSourceRead(read: Omit<WorkSourceRead, 'available' | 'readVersion'>): WorkSourceRead {
  const token = { ...read, available: false, readVersion: ++nextReadVersion }
  reportWorkSourceRead(token)

  return token
}

export function reportWorkSourceRead(read: WorkSourceRead): void {
  if (!read.connectionId || !read.profile || !read.storedSessionId) {
    return
  }

  const k = key(read)
  const previous = $unavailableWorkSources.get()
  const old = previous[k]
  const epoch = Number.isSafeInteger(read.authorityEpoch) && read.authorityEpoch! > 0 ? read.authorityEpoch : undefined

  // Monotonic captured epochs fence replies after a newer read has started.
  if (epoch !== undefined && old?.authorityEpoch !== undefined && epoch < old.authorityEpoch) {
    return
  }

  if (old?.readVersion !== undefined && (read.readVersion === undefined || read.readVersion < old.readVersion)) {
    return
  }

  const available = read.available && epoch !== undefined

  if (old?.available === available && old.authorityEpoch === epoch && old.readVersion === read.readVersion) {
    return
  }

  $unavailableWorkSources.set({ ...previous, [k]: { available, authorityEpoch: epoch, readVersion: read.readVersion } })
}

export function workSourceUnavailable(
  connectionId: string | undefined,
  profile: string | undefined,
  ids: readonly string[],
  observations: Observations = $unavailableWorkSources.get()
): boolean {
  if (!connectionId || !profile) {
    return false
  }

  return ids.some(storedSessionId =>
    requiredSources.some(
      source => observations[key({ connectionId, profile, source, storedSessionId })]?.available === false
    )
  )
}

/** Missing, pending, failed and obsolete reads cannot authorize idle green.
 * Completion itself still comes from the outcome/unfinished-work derivation. */
export function workSourceAuthorityComplete(
  connectionId: string | undefined,
  profile: string | undefined,
  ids: readonly string[],
  authority: WorkSourceAuthority,
  observations: Observations = $unavailableWorkSources.get()
): boolean {
  if (!connectionId || !profile || !ids.length || !authority.durable || !authority.runtime) {
    return false
  }

  if (workSourceUnavailable(connectionId, profile, ids, observations)) {
    return false
  }

  return ids.some(storedSessionId =>
    requiredSources.every(source => {
      const observation = observations[key({ connectionId, profile, storedSessionId, source })]
      const expected = source === 'lineage' || source === 'tasks' ? authority.durable : authority.runtime

      return observation?.available === true && observation.authorityEpoch === expected
    })
  )
}
