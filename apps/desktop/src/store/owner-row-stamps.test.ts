import { beforeEach, describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/hermes'

import {
  $ownerRowStamps,
  _resetOwnerRowStampsForTests,
  refreshOwnerRowStamps,
  rowsNeedingOwnerStamps,
  STAMP_FAILURE_BACKOFF_MS,
  STAMP_FRESH_MS,
  type StampFetcher
} from './owner-row-stamps'

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({
    connection_id: 'local',
    id,
    offline_cached: false,
    offline_synced_at: 9_000,
    profile: 'default',
    ...extra
  }) as SessionInfo

function recorder(
  answer: (ids: string[]) => Partial<SessionInfo>[] = ids => ids.map(id => ({ id, kanban_active: 1 }))
) {
  const calls: [string, string, string[]][] = []

  const fetcher: StampFetcher = async (connectionId, profile, ids) => {
    calls.push([connectionId, profile, ids])

    return answer(ids)
  }

  return { calls, fetcher }
}

beforeEach(() => _resetOwnerRowStampsForTests())

describe('owner row stamps', () => {
  it('only awake replica rows need stamps (not tree/live rows, not asleep cached rows)', () => {
    const rows = [row('awake'), row('asleep', { offline_cached: true }), row('tree', { offline_synced_at: undefined })]

    expect(rowsNeedingOwnerStamps(rows).map(r => r.id)).toEqual(['awake'])
  })

  it('batches per owner (connection + profile) and keys answers by owner identity', async () => {
    const { calls, fetcher } = recorder()

    await refreshOwnerRowStamps(
      [row('a'), row('b'), row('c', { profile: 'work' }), row('d', { connection_id: 'mini' })],
      fetcher,
      1
    )

    expect(calls).toEqual([
      ['local', 'default', ['a', 'b']],
      ['local', 'work', ['c']],
      ['mini', 'default', ['d']]
    ])
    expect([...$ownerRowStamps.get().keys()].sort()).toEqual(
      [
        'local\u0000default\u0000a',
        'local\u0000default\u0000b',
        'local\u0000work\u0000c',
        'mini\u0000default\u0000d'
      ].sort()
    )
    expect($ownerRowStamps.get().get('local\u0000default\u0000a')).toEqual({ kanban_active: 1 })
  })

  it('re-asks a row only after STAMP_FRESH_MS', async () => {
    const { calls, fetcher } = recorder()

    await refreshOwnerRowStamps([row('a')], fetcher, 1_000)
    await refreshOwnerRowStamps([row('a')], fetcher, 1_000 + STAMP_FRESH_MS - 1)
    expect(calls).toHaveLength(1)

    await refreshOwnerRowStamps([row('a')], fetcher, 1_000 + STAMP_FRESH_MS)
    expect(calls).toHaveLength(2)
  })

  it('an owner whose request failed (older backend, unreachable) backs off and keeps prior stamps', async () => {
    await refreshOwnerRowStamps([row('a')], recorder().fetcher, 0)

    let failures = 0

    const failing: StampFetcher = async () => {
      failures += 1

      throw new Error('404')
    }

    await refreshOwnerRowStamps([row('a')], failing, STAMP_FRESH_MS)
    await refreshOwnerRowStamps([row('a')], failing, STAMP_FRESH_MS + STAMP_FAILURE_BACKOFF_MS - 1)
    expect(failures).toBe(1)
    expect($ownerRowStamps.get().get('local\u0000default\u0000a')).toEqual({ kanban_active: 1 })

    await refreshOwnerRowStamps([row('a')], failing, STAMP_FRESH_MS + STAMP_FAILURE_BACKOFF_MS)
    expect(failures).toBe(2)
  })

  it('splits more than 100 ids into several requests', async () => {
    const { calls, fetcher } = recorder()
    const rows = Array.from({ length: 150 }, (_, i) => row(`s${i}`))

    await refreshOwnerRowStamps(rows, fetcher, 1)

    expect(calls.map(([, , ids]) => ids.length)).toEqual([100, 50])
  })
})
