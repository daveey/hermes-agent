import { translateNow } from '@/i18n/runtime'
import { reconnectGateway } from '@/store/gateway-reconnect'
import { notifyError } from '@/store/notifications'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { $connection } from '@/store/session'

let reloading = false

/**
 * Fork (`app.reloadAll`, ⇧⌘R): restart the Python backend for the profile in
 * view, then reload the window — so a core or plugin edit is picked up without
 * quitting the app. Remote gateways aren't ours to restart, so they only get a
 * reconnect before the reload. Restarting the backend interrupts every turn it
 * is running. A failed restart still reloads the window (the new renderer boot
 * restarts a dead backend itself).
 */
export async function reloadAppAndBackend(reload: () => void = () => window.location.reload()): Promise<void> {
  if (reloading) {
    return
  }

  reloading = true

  try {
    if ($connection.get()?.mode === 'remote') {
      await reconnectGateway()
    } else {
      await window.hermesDesktop?.recycleBackend?.(normalizeProfileKey($activeGatewayProfile.get()))
    }
  } catch (err) {
    notifyError(err, translateNow('notifications.errors.restartHermesFailed'))
  } finally {
    reloading = false
    reload()
  }
}
