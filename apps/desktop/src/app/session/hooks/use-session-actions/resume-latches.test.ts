import { afterEach, describe, expect, it } from 'vitest'

import { $notifications, clearNotifications } from '@/store/notifications'

import { clearResumeLatches, notifyResumeFailed, takeResumeFailure } from './resume-latches'

// A quiet re-attach attempt: records the failure without a toast.
const fail = (storedSessionId: string, error: unknown = new Error(`resume failed: ${storedSessionId}`)) =>
  notifyResumeFailed(error, 'Resume failed', storedSessionId, { reattach: true })

afterEach(() => {
  for (let n = 0; n <= 70; n += 1) {
    takeResumeFailure(`stored-${n}`)
  }

  clearNotifications()
})

describe('the error a swallowed resume failure leaves for its caller', () => {
  it('is handed over once', () => {
    const error = new Error('request timed out after 30s: session.resume')
    fail('stored-0', error)

    expect(takeResumeFailure('stored-0')).toEqual({ error })
    expect(takeResumeFailure('stored-0')).toBeUndefined()
    expect($notifications.get()).toEqual([])
  })

  it('is retired by the next resume attempt of that chat', () => {
    fail('stored-0')
    clearResumeLatches('stored-0')

    expect(takeResumeFailure('stored-0')).toBeUndefined()
  })

  it('keeps the 64 newest chats, so errors nobody reads do not pile up', () => {
    for (let n = 0; n <= 64; n += 1) {
      fail(`stored-${n}`)
    }

    expect(takeResumeFailure('stored-0')).toBeUndefined()

    // A chat that fails again becomes the newest, so the next new chat evicts 2, not 1.
    fail('stored-1')
    fail('stored-65')

    expect(takeResumeFailure('stored-2')).toBeUndefined()
    expect(takeResumeFailure('stored-1')).toBeDefined()
    expect(takeResumeFailure('stored-3')).toBeDefined()
    expect(takeResumeFailure('stored-65')).toBeDefined()
  })
})
