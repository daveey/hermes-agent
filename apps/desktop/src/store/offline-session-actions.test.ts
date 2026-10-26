import { beforeEach, describe, expect, it, vi } from 'vitest'

import { makeSessionInfo } from '@/test/session-info'

const hubRequest = vi.fn(async () => ({ id: 'r1', queued_locally: true, state: 'local' }))

vi.mock('./hub-sync', () => ({ hubRequest }))

const { offlineActionRequestBody, offlineOpenNotice, queueOfflineSessionAction } = await import(
  './offline-session-actions'
)

const { $offlineSessions, _resetOfflineSessionsForTests, withoutLocallyArchived } = await import('./offline-sessions')

const row = makeSessionInfo({
  connection_id: 'conn-mini',
  id: 's1',
  offline_cached: true,
  offline_gateway: 'mini',
  offline_synced_at: 100,
  offline_version: 7,
  profile: 'work',
  title: 'T'
})

beforeEach(() => {
  _resetOfflineSessionsForTests()
  hubRequest.mockClear()
  $offlineSessions.set([row])
})

describe('offline owner actions', () => {
  it('builds the owner request from gateway, profile/id key and replica version', () => {
    expect(offlineActionRequestBody(row, 'rename', { title: 'New' })).toEqual({
      action: 'rename',
      args: { title: 'New' },
      base_version: 7,
      coll: 'sessions',
      gateway: 'mini',
      key: 'work/s1'
    })
  })

  it('archive queues the request and hides the row until the owner applies it', async () => {
    await queueOfflineSessionAction(row, 'archive')

    expect(hubRequest).toHaveBeenCalledWith(expect.objectContaining({ action: 'archive', gateway: 'mini' }))
    expect($offlineSessions.get()).toEqual([])
    expect(withoutLocallyArchived([row])).toEqual([])
  })

  it('rename does not hide the row', async () => {
    await queueOfflineSessionAction(row, 'rename', { title: 'X' })

    expect($offlineSessions.get()).toHaveLength(1)
  })

  it('says plainly why an open is refused', () => {
    expect(offlineOpenNotice(row)).toMatch(/mini is offline.*can't be opened/)
  })
})
