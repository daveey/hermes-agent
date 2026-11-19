import type { SessionInfo } from '@/types/hermes'

export type MuteRow = Pick<SessionInfo, 'id'> &
  Partial<Pick<SessionInfo, '_lineage_root_id' | 'connection_id' | 'muted' | 'profile'>>

export const parkRows = new Map<string, MuteRow>()

export function resetParkRowsForTests(): void {
  parkRows.clear()
}
