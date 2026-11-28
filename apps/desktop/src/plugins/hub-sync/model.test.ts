import { describe, expect, it } from 'vitest'

import { type HubSessionRow, isRowPending, markRowPending, replicaSessionsToShow, sessionRequestBody } from './model'

// [daveey] Sidebar rows for sessions on gateways that aren't live right now,
// read from the window gateway's hermes-hub replica (hub-sync plugin REST).

const row = (gateway: string, id: string, extra: Partial<HubSessionRow> = {}): HubSessionRow => ({
  gateway,
  id,
  key: `default/${id}`,
  local: false,
  profile: 'default',
  stale: false,
  synced_at: 1_000,
  title: id,
  version: 3,
  ...extra
})

describe('replicaSessionsToShow', () => {
  it('shows only gateways that are not live, never the local gateway', () => {
    const hub = [row('mac', 's_local', { local: true }), row('mini', 's_mini'), row('cloud', 's_cloud')]
    const shown = replicaSessionsToShow(hub, [], gw => gw === 'cloud')

    expect(shown.map(r => r.id)).toEqual(['s_mini'])
  })

  it('keeps live data primary: a row the live list already has for that gateway is dropped', () => {
    const hub = [row('mini', 's_a'), row('mini', 's_b')]
    const live = [{ gateway: 'mini', id: 's_a' }]

    expect(replicaSessionsToShow(hub, live, () => false).map(r => r.id)).toEqual(['s_b'])
  })

  it('dedupes by (gateway, session id): same id on another gateway is a different row', () => {
    const hub = [row('mini', 's_x'), row('mini', 's_x', { key: 'bb/s_x' }), row('cloud', 's_x')]
    const live = [{ gateway: 'laptop', id: 's_x' }]

    expect(replicaSessionsToShow(hub, live, () => false).map(r => `${r.gateway}:${r.id}`)).toEqual([
      'mini:s_x',
      'cloud:s_x'
    ])
  })

  it('drops archived and hidden replica rows', () => {
    const hub = [row('mini', 's_a', { archived: true }), row('mini', 's_b', { hidden: true }), row('mini', 's_c')]

    expect(replicaSessionsToShow(hub, [], () => false).map(r => r.id)).toEqual(['s_c'])
  })
})

describe('owner requests', () => {
  it('builds a sessions request for the owning gateway with the record version', () => {
    expect(sessionRequestBody(row('mini', 's_a'), 'rename', { title: 'New' })).toEqual({
      action: 'rename',
      args: { title: 'New' },
      base_version: 3,
      coll: 'sessions',
      gateway: 'mini',
      key: 'default/s_a'
    })
  })

  it('marks a row pending optimistically and reads the marker back', () => {
    const rows = [row('mini', 's_a'), row('mini', 's_b')]
    const next = markRowPending(rows, rows[1], { action: 'archive', id: 'r1', state: 'local' })

    expect(isRowPending(next[0])).toBe(false)
    expect(isRowPending(next[1])).toBe(true)
    expect(next[1].pending?.[0].action).toBe('archive')
    expect(rows[1].pending).toBeUndefined()
  })
})

it('keeps same-id sessions in different profiles independent', () => {
  const rows = [row('mini', 'same'), row('mini', 'same', { profile: 'work', key: 'work/same' })]
  expect(replicaSessionsToShow(rows, [], () => false)).toHaveLength(2)
  expect(replicaSessionsToShow(rows, [{ gateway: 'mini', profile: 'work', id: 'same' }], () => false)).toEqual([
    rows[0]
  ])
})
