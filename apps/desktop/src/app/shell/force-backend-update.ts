import { forceHermesUpdate, getForceUpdateState } from '@/api/system'
import type { ForceUpdateCopy } from '@/i18n/types_force_update'
import { confirm } from '@/store/confirm'
import { notify, notifyError } from '@/store/notifications'

/** [daveey] "Force update" from the backend version pill's context menu: read how many turns forcing the host's
 *  pending core update would stop, confirm, then ask hermes-sync to apply it now (`POST /api/hermes/update/force`).
 *  When no update is owed the server's reason is shown and nothing is asked. */
export async function forceBackendUpdate(copy: ForceUpdateCopy): Promise<void> {
  let state: Awaited<ReturnType<typeof getForceUpdateState>>

  try {
    state = await getForceUpdateState()
  } catch (error) {
    notifyError(error, copy.failed)

    return
  }

  if (!state.available) {
    notify({ kind: 'warning', message: state.message || copy.failed })

    return
  }

  const confirmed = await confirm({
    cancelLabel: copy.cancelLabel,
    confirmLabel: copy.confirmLabel,
    description: copy.confirmBody(state.running_turns),
    destructive: true,
    title: copy.confirmTitle
  })

  if (!confirmed) {
    return
  }

  try {
    await forceHermesUpdate()
    notify({ kind: 'success', message: copy.requested })
  } catch (error) {
    notifyError(error, copy.failed)
  }
}
