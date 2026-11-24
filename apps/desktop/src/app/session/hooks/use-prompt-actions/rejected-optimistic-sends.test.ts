import { afterEach, describe, expect, it } from 'vitest'

import { en } from '@/i18n/en'
import { clearNotifications } from '@/store/notifications'

import {
  rejectSendOnFailedResume,
  resetRejectedOptimisticSendsForTests,
  startOptimisticSend,
  withoutRejectedOptimisticSends
} from './rejected-optimistic-sends'

const reject = (optimisticId: string) =>
  rejectSendOnFailedResume(new Error('request timed out after 30s: session.resume'), {
    copy: en.desktop,
    drift: null,
    optimisticId,
    // A queue drain: marks the bubble without a notice.
    options: { fromQueue: true },
    uncertain: false
  })

const shown = (...ids: string[]) => withoutRejectedOptimisticSends(ids.map(id => ({ id }))).map(row => row.id)

afterEach(() => {
  resetRejectedOptimisticSendsForTests()
  clearNotifications()
})

describe('rejected optimistic sends', () => {
  it('drops a rejected bubble from a snapshot and leaves the other rows alone', () => {
    reject('user-a')

    expect(shown('stored-1', 'user-a', 'user-b')).toEqual(['stored-1', 'user-b'])
  })

  it('a retry under the same submission id makes its bubble live again', () => {
    reject('user-a')

    expect(startOptimisticSend('a')).toBe('user-a')
    expect(shown('user-a')).toEqual(['user-a'])
  })

  it('remembers the 64 newest rejections', () => {
    for (let n = 0; n <= 64; n += 1) {
      reject(`user-${n}`)
    }

    expect(shown('user-0', 'user-1', 'user-64')).toEqual(['user-0'])

    // Rejecting a remembered id again makes it the newest, so the next new one evicts another.
    reject('user-1')
    reject('user-65')

    expect(shown('user-1', 'user-2', 'user-3', 'user-65')).toEqual(['user-2'])
  })
})
