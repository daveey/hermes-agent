import { beforeEach, expect, it } from 'vitest'

import { $sessions } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

import { sessionActionRow } from './offline-session-owner'
import { $offlineSessions, _resetOfflineSessionsForTests } from './offline-sessions'

beforeEach(() => {
  $sessions.set([])
  _resetOfflineSessionsForTests()
})

it('resolves a cached ancestor to its reconnected live tip on the same owner only', () => {
  const cached = makeSessionInfo({
    id: 'root',
    connection_id: 'mini',
    profile: 'work',
    offline_cached: true,
    offline_gateway: 'mini'
  })
  const tip = makeSessionInfo({
    id: 'tip',
    connection_id: 'mini',
    profile: 'work',
    _lineage_ids: ['root', 'tip'],
    _lineage_root_id: 'root'
  })
  $offlineSessions.set([cached])
  $sessions.set([{ ...tip, connection_id: 'other' }, tip])
  expect(sessionActionRow('root', cached)).toEqual(tip)
})
