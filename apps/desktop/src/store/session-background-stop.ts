/**
 * [daveey] Stop a live background process from its sidebar row's process
 * badge. The backend kills it (see `stopSessionBackgroundProcess`); on success
 * the process leaves the row at once rather than on the next list poll, so a
 * row held only by that process goes green the moment Stop confirms.
 */

import { stopSessionBackgroundProcess } from '@/api/sessions'
import type { SessionInfo } from '@/types/hermes'

import { notifyError } from './notifications'
import { setMessagingSessions, setSessions } from './session'
import { withoutBackgroundProcess } from './session-dot-state'

const dropProcess = (rows: SessionInfo[], processId: string): SessionInfo[] => {
  let changed = false

  const next = rows.map(row => {
    const updated = withoutBackgroundProcess(row, processId)
    changed ||= updated !== row

    return updated
  })

  return changed ? next : rows
}

/** Resolves true when the process is gone (killed now or already exited). */
export async function stopRowBackgroundProcess(row: SessionInfo, processId: string): Promise<boolean> {
  try {
    await stopSessionBackgroundProcess(row, processId)
  } catch (error) {
    notifyError(error, 'Could not stop the process')

    return false
  }

  setSessions(rows => dropProcess(rows, processId))
  setMessagingSessions(rows => dropProcess(rows, processId))

  return true
}
