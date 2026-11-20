import type { ForceUpdateCopy } from './types_force_update'

export const enForceUpdate: ForceUpdateCopy = {
  menuItem: 'Force update',
  confirmTitle: 'Force the backend update now?',
  confirmBody: runningTurns =>
    `${
      runningTurns === 0
        ? 'No turns are running right now.'
        : `${runningTurns} running turn${runningTurns === 1 ? '' : 's'} will be stopped.`
    } Turns that can resume continue after the update; others need to be sent again.`,
  confirmLabel: 'Force update',
  cancelLabel: 'Cancel',
  requested: 'Update requested; the backend restarts within a few minutes',
  failed: 'Could not force the backend update'
}
