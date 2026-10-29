import { $sessions } from '@/store/session'
import {
  $blockedSessionIds,
  $muteWriteGuard,
  isSessionBlocked,
  muteWriteFor,
  resolveSessionParked
} from '@/store/session-blocked'
import type { SessionInfo } from '@/types/hermes'

import { useStoresSelector } from './use-session-slice'

/** [daveey] Whether a row is parked (⌘B): a fresh ⌘B write, else the backend's `row.muted`, else the local list.
 *  Re-renders only when that boolean flips. */
export function useSessionParked(
  id: null | string | undefined,
  row: null | Partial<Pick<SessionInfo, 'muted'>> | undefined
): boolean {
  return useStoresSelector([$blockedSessionIds, $muteWriteGuard, $sessions], () =>
    id
      ? resolveSessionParked(
          isSessionBlocked(id, $blockedSessionIds.get(), $sessions.get()),
          row,
          muteWriteFor(id, $muteWriteGuard.get(), $sessions.get())
        )
      : false
  )
}
