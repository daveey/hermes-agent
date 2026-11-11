import { afterEach, describe, expect, it } from 'vitest'

import { $gatewayActivationEpoch } from '@/store/gateway-activation-epoch'

import {
  backendRefusesSubmissionId,
  noteSubmissionIdAccepted,
  noteSubmissionIdRefused,
  resetSubmissionIdSupportForTests,
  SUBMISSION_ID_REFUSAL_TTL_MS,
  submissionIdentityEpoch
} from './submission-identity-support'

afterEach(() => {
  resetSubmissionIdSupportForTests()
  $gatewayActivationEpoch.set(0)
})

describe('submission identity support memo', () => {
  it('remembers a refusal per destination only', () => {
    noteSubmissionIdRefused('["hub","default"]', submissionIdentityEpoch(), 1000)

    expect(backendRefusesSubmissionId('["hub","default"]', 1001)).toBe(true)
    expect(backendRefusesSubmissionId('["hub","alpha"]', 1001)).toBe(false)
  })

  it('expires so an upgraded backend gets identity back', () => {
    noteSubmissionIdRefused('k', submissionIdentityEpoch(), 0)

    expect(backendRefusesSubmissionId('k', SUBMISSION_ID_REFUSAL_TTL_MS - 1)).toBe(true)
    expect(backendRefusesSubmissionId('k', SUBMISSION_ID_REFUSAL_TTL_MS)).toBe(false)
  })

  it('an accepted identity clears the refusal', () => {
    noteSubmissionIdRefused('k', submissionIdentityEpoch(), 0)
    noteSubmissionIdAccepted('k')

    expect(backendRefusesSubmissionId('k', 1)).toBe(false)
  })

  it('a gateway activation (route/source swap) invalidates the refusal', () => {
    $gatewayActivationEpoch.set(4)
    noteSubmissionIdRefused('k', submissionIdentityEpoch(), 0)

    expect(backendRefusesSubmissionId('k', 1)).toBe(true)

    $gatewayActivationEpoch.set(5)

    expect(backendRefusesSubmissionId('k', 2)).toBe(false)
  })

  it('a refusal answered after a swap is not stamped on the new backend', () => {
    $gatewayActivationEpoch.set(4)
    const sentUnder = submissionIdentityEpoch()

    $gatewayActivationEpoch.set(5)
    noteSubmissionIdRefused('k', sentUnder, 0)

    expect(backendRefusesSubmissionId('k', 1)).toBe(false)
  })
})
