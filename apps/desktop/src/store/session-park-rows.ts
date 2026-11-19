import type { SessionInfo } from '@/types/hermes'

export type MuteRow = Pick<SessionInfo, 'id'> &
  Partial<Pick<SessionInfo, '_lineage_root_id' | 'connection_id' | 'muted' | 'profile'>>

export const parkRows = new Map<string, MuteRow>()
let listedRows: readonly MuteRow[] | undefined
let lineageRows: MuteRow[] | undefined

/** Preserve the array identity used by lineageAliases' index memo between renders. */
export function parkLineageRows(sessions: readonly MuteRow[]): MuteRow[] {
  if (!lineageRows || listedRows !== sessions) {
    listedRows = sessions
    lineageRows = [...parkRows.values(), ...sessions]
  }

  return lineageRows
}

export function cacheParkRow(row: MuteRow): void {
  parkRows.delete(row.id)
  parkRows.set(row.id, row)

  if (parkRows.size > 1000) {
    parkRows.delete(parkRows.keys().next().value!)
  }

  lineageRows = undefined
}

export function resetParkRowsForTests(): void {
  parkRows.clear()
  listedRows = undefined
  lineageRows = undefined
}
