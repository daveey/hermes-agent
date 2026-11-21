import type { ForceUpdateCopy } from './types_force_update'

export const enForceUpdate: ForceUpdateCopy = {
  menuItem: 'Force update',
  confirmTitle: 'Force the backend update now?',
  confirmBody: runningTurns =>
    `${
      runningTurns === null
        ? 'Hermes could not tell how many turns are running.'
        : runningTurns === 0
          ? 'No turns are running right now.'
          : `${runningTurns} turn${runningTurns === 1 ? ' is' : 's are'} running now.`
    } The update stops Desktop and terminal turns at their next step: those that can resume continue after the update; others need to be sent again. Messaging, API and scheduled-job turns are not stopped: the update waits for them to finish.`,
  confirmLabel: 'Force update',
  cancelLabel: 'Cancel',
  requested: 'Update requested; the backend restarts within a few minutes',
  failed: 'Could not force the backend update',
  syncedTitle: 'hermes-sync is updating Hermes',
  syncedApplying:
    'The update applies within a few minutes. Busy chats pause at their next step and continue on the new version; you can keep working. If it never lands, run `hermes update` in a terminal.'
}
