import { useEffect } from 'react'

import { $sessions } from '@/store/session'
import {
  $blockedSessionIds,
  $muteWriteGuard,
  isSessionBlocked,
  MUTE_WRITE_GUARD_MS,
  type MuteRow,
  muteWriteFor,
  rememberParkRow,
  resolveSessionParked
} from '@/store/session-blocked'

import { useStoresSelector } from './use-session-slice'

/** [daveey] Whether a row is parked (⌘B): a fresh ⌘B write, else the backend's `row.muted`, else the local list.
 *  Selects this session's park boolean and write lifecycle, not unrelated rows' writes. */
export function useSessionParked(id: null | string | undefined, row: MuteRow | null | undefined): boolean {
  const write = useStoresSelector([$muteWriteGuard, $sessions], () => (id ? muteWriteFor(id) : undefined))

  useEffect(() => {
    if (!row) {
      return
    }

    rememberParkRow(row)
    const remaining = write ? write.at + MUTE_WRITE_GUARD_MS - Date.now() : 0

    // A signature-stable agreeing page may never render again. Reconcile once
    // the stale-page window closes, cancelling if the row or write changes.
    if (write?.acked && row.muted === write.value && remaining > 0) {
      const timer = setTimeout(() => rememberParkRow(row), remaining)

      return () => clearTimeout(timer)
    }
  }, [row, write])

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
