import type { NotificationAction } from '@/store/notifications'

import { isDefinitiveSessionMutationRefusal } from '../../../shared/src/session-http-mutations'

/** [daveey] The toast button for a failed session edit (archive, rename): a Retry that replays
 *  the SAME intent on the SAME owner, offered only while the mutation client still holds that
 *  intent's identity. A definitive refusal (conflict, not found, permission) drops the identity,
 *  so there is nothing to replay and no button: the user re-reads the row instead. A 503 owner
 *  stall, a timeout or a lost reply keeps it, so the retry carries the same request_id and
 *  expected revision and the owner answers an already-committed edit from its receipt. */
export function sessionWriteRetryAction(
  error: unknown,
  label: string,
  retry: () => void
): NotificationAction | undefined {
  if (isDefinitiveSessionMutationRefusal(error)) {
    return undefined
  }

  return { label, onClick: retry }
}
