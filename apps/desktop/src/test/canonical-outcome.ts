import type { CanonicalStripe, SessionOutcome, SessionStatusEnvelope } from '@/types/session-outcome'

/** A stored outcome carrying a complete `status_version` 1 envelope (`outcome.status`); `presentation.stripe` is the
 *  only status input a surface reads. `kind` is deliberately independent explanatory text, so tests can prove it
 *  never selects a stripe. `envelope` overrides envelope fields, `outcome` the surrounding explanation. */
export function canonicalOutcome(
  stripe: CanonicalStripe,
  envelope: Partial<SessionStatusEnvelope> = {},
  outcome: Partial<SessionOutcome> = {}
): SessionOutcome {
  return {
    kind: 'unresolved',
    ...outcome,
    status: {
      status_version: 1,
      kind: ({ done: 'done', attention: 'unresolved', blocked: 'muted', stuck: 'decision', waiting: 'waiting', none: 'running' } as const)[stripe],
      revision: 7,
      generation: 3,
      identity: { gateway: 'gateway', profile: 'default', session_id: 'session', owner_epoch: 'epoch' },
      coverage: { state: 'complete', reasons: [] },
      presentation: { stripe, reason_codes: [`test_${stripe}`] },
      ...envelope
    }
  }
}
