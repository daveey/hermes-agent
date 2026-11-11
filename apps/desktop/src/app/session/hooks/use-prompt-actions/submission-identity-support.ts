/** Per-destination memo of a backend that refused `prompt.submit`'s `submission_id` before
 * admission (`hermes serve`'s tui_gateway contract has no such field and answers 4000).
 * Without it every send paid a refused request plus the identityless resend: one extra
 * round trip on each message. Keyed by the submission destination's scopeKey (connection +
 * profile). The key carries no backend identity, so an entry is only valid inside the gateway
 * activation epoch it was recorded in (a route/source swap may land on an identity-capable
 * backend under the same key), and it expires so an in-place upgrade gets identity back. */

import { $gatewayActivationEpoch } from '@/store/gateway-activation-epoch'

export const SUBMISSION_ID_REFUSAL_TTL_MS = 10 * 60_000

const refusals = new Map<string, { at: number; epoch: number }>()

const currentEpoch = () => {
  const epoch = $gatewayActivationEpoch.get()

  return Number.isFinite(epoch) ? epoch : 0
}

/** Capture before sending an identified request; pass to noteSubmissionIdRefused. */
export const submissionIdentityEpoch = currentEpoch

export function backendRefusesSubmissionId(scopeKey: string, now = Date.now()): boolean {
  const refusal = refusals.get(scopeKey)

  if (refusal === undefined) {
    return false
  }

  if (now - refusal.at >= SUBMISSION_ID_REFUSAL_TTL_MS || refusal.epoch !== currentEpoch()) {
    refusals.delete(scopeKey)

    return false
  }

  return true
}

/** `requestEpoch` is the activation the refused request was SENT under. A refusal that arrives
 * after a swap describes the previous backend, so it is dropped rather than stamped on the new one. */
export function noteSubmissionIdRefused(scopeKey: string, requestEpoch: number, now = Date.now()): void {
  if (requestEpoch !== currentEpoch()) {
    return
  }

  refusals.set(scopeKey, { at: now, epoch: requestEpoch })
}

export function noteSubmissionIdAccepted(scopeKey: string): void {
  refusals.delete(scopeKey)
}

export function resetSubmissionIdSupportForTests(): void {
  refusals.clear()
}
