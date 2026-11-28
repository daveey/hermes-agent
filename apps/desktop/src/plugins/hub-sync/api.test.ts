import { beforeEach, describe, expect, it, vi } from 'vitest'

// [daveey] instant loads: the replica rows come from a local read and must never wait on the per-gateway reachability
// probes (each one is an SSH round trip); a live owner list hides them only once it is fresh.

const sdk = vi.hoisted(() => ({
  down: false,
  fresh: new Set<string>(),
  statusCalls: 0,
  unavailable: [] as string[]
}))

const ROW = (gateway: string, id: string) => ({
  gateway,
  id,
  key: `default/${id}`,
  profile: 'default',
  title: id,
  message_count: 3
})

vi.mock('@hermes/plugin-sdk', () => ({
  connectionForHubGateway: (g: string) => ({ hub: 'hermes-hub', mini: 'mac-mini' })[g],
  host: { state: { gateways: { get: () => [{ id: 'local' }, { id: 'mac-mini' }, { id: 'hermes-hub' }] } } },
  hubGatewayForConnection: (c: string) => (c === 'local' ? 'macbook' : undefined),
  hubGet: vi.fn(async (path: string, conn: string) => {
    if (path.startsWith('/sessions')) {
      if (sdk.down) {
        return null
      }

      return { sessions: [ROW('mini', 's_m'), ROW('hub', 's_h'), ROW('macbook', 's_l')] }
    }

    sdk.statusCalls++

    return new Promise(() => {}) // a reachability probe that never answers (an SSH dial in progress)
  }),
  hubReplicaConnectionId: () => 'local',
  recordSessionGatewayUnavailable: (c: string) => void sdk.unavailable.push(c),
  sessionOwnerIsFresh: (c: string) => sdk.fresh.has(c)
}))

vi.mock('./model', () => ({ replicaSessionsToShow: (rows: unknown[]) => rows }))

import { loadReplicaSidebarRows } from './api'

describe('loadReplicaSidebarRows', () => {
  beforeEach(() => {
    sdk.down = false
    sdk.fresh.clear()
    sdk.statusCalls = 0
  })

  it('returns replica rows of every remote gateway without waiting on reachability probes', async () => {
    const rows = await Promise.race([
      loadReplicaSidebarRows(),
      new Promise<'timeout'>(resolve => setTimeout(() => resolve('timeout'), 500))
    ])

    expect(rows).not.toBe('timeout')
    expect((rows as Array<{ id: string }>).map(r => r.id).sort()).toEqual(['s_h', 's_m'])
    expect(sdk.statusCalls).toBeGreaterThan(0) // probes still run, in the background
  })

  it("hides a gateway's replica rows once its live list is fresh", async () => {
    sdk.fresh.add('mac-mini')
    const rows = (await loadReplicaSidebarRows()) as Array<{ id: string }>

    expect(rows.map(r => r.id)).toEqual(['s_h'])
  })

  it('answers null (publish nothing) when the replica read has no answer, never an empty list', async () => {
    sdk.down = true

    expect(await loadReplicaSidebarRows()).toBeNull()
  })
})
