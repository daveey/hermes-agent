import { translateNow } from '@/i18n'
import { servedByHermesCloud } from '@/lib/hermes-cloud-host'
import { persistString, storedString } from '@/lib/storage'

import type { NotificationInput } from './notifications'

// Client-install-wide recovery note, not a queued edit or a claim about the active gateway.
// Store only an acknowledgement bit: no session IDs, error text or remote credentials.
const KEY = 'hermes:client-install:upgrade-required-note'

export function hasClientUpgradeNote(): boolean {
  return storedString(KEY) === '1'
}

export function clientUpgradeNote(): NotificationInput {
  persistString(KEY, '1')

  return {
    id: 'client-upgrade-required',
    kind: 'error',
    persistent: true,
    message: translateNow('notifications.errors.rpcOutOfSync'),
    action: servedByHermesCloud()
      ? { label: translateNow('commandCenter.reloadWindow'), onClick: () => window.location.reload(), dismiss: false }
      : {
          label: translateNow('notifications.updateHermes'),
          dismiss: false,
          onClick: () => void import('@/store/updates').then(({ openUpdatesWindow }) => openUpdatesWindow('client'))
        },
    // Only an explicit acknowledgement removes the reminder. Opening Updates or an
    // unrelated successful request is not proof the refused edit is now compatible.
    onDismiss: () => persistString(KEY, null)
  }
}
