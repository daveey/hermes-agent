import { useEffect } from 'react'

import { $sessions } from '@/store/session'
import {
  $blockedSessionIds,
  $muteWriteGuard,
  isSessionBlocked,
  type MuteRow,
  muteWriteFor,
  rememberParkRow,
  resolveSessionParked
} from '@/store/session-blocked'

import { useStoresSelector } from './use-session-slice'

/** [daveey] Whether a row is parked (⌘B): a fresh ⌘B write, else the backend's `row.muted`, else the local list.
 *  Re-renders only when that boolean flips. */
export function useSessionParked(id: null | string | undefined, row: MuteRow | null | undefined): boolean {
  useEffect(() => {
    if (row) {
      rememberParkRow(row)
    }
  }, [row])

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
