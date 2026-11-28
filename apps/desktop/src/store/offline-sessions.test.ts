import { beforeEach, describe, expect, it } from 'vitest'

import { buildGatewaySessionGroups } from '@/app/chat/sidebar/gateway-group-model'
import { filterSessionsByProfileScope } from '@/app/chat/sidebar/profile-scope'
import { buildSessionByAnyId, resolvePinnedSessions } from '@/app/chat/sidebar/session-index'
import type { SessionInfo } from '@/hermes'
import { sessionOnHiddenGateway } from '@/store/gateway-visibility'
import { $pinnedSessionIds, $sidebarShowAutomated } from '@/store/layout'
import { sessionHiddenAsAutomated } from '@/store/sidebar-automated'

import {
  _resetOfflineSessionsForTests,
  hideOfflineSession,
  mergeOfflineFallback,
  type OfflineReplicaRecord,
  offlineSessionsFromReplica,
  recordSessionGatewayUnavailable,
  recordSessionListResult,
  sessionOwnerIsFresh,
  staleSessionOwners,
  syncedAgoLabel
} from './offline-sessions'
import {
  $ownerRowStamps,
  _resetOwnerRowStampsForTests,
  refreshOwnerRowStamps,
  type StampFetcher
} from './owner-row-stamps'
import { kanbanStripeCounts } from './session-dot-state'

// [daveey] Cached (hermes-hub replica) sessions of an unreachable gateway feed
// the NORMAL sidebar pipeline as fallback rows; nothing here is a second list.

const rec = (id: string, extra: Partial<OfflineReplicaRecord> = {}): OfflineReplicaRecord => ({
  activity: 5_000,
  automated: false,
  gateway: 'mini',
  id,
  key: `default/${id}`,
  local: false,
  message_count: 4,
  profile: 'default',
  source: 'desktop',
  started_at: 4_000,
  stale: false,
  synced_at: 9_000,
  title: `Chat ${id}`,
  version: 1,
  ...extra
})

const connFor = (gateway: string) => (gateway === 'mini' ? 'conn-mini' : gateway === 'cloud' ? 'conn-cloud' : undefined)

const live = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: false,
    last_active: 1,
    message_count: 3,
    model: null,
    output_tokens: 0,
    preview: null,
    source: 'desktop',
    started_at: 1,
    title: id,
    tool_call_count: 0,
    ...extra
  }) as SessionInfo

const ids = (rows: readonly SessionInfo[]) => rows.map(r => r.id)

beforeEach(() => {
  _resetOfflineSessionsForTests()
  $sidebarShowAutomated.set(false)
  $pinnedSessionIds.set([])
})

describe('offlineSessionsFromReplica', () => {
  it('turns an ordinary user chat into a normal row marked offline, routed to its gateway+profile', () => {
    const [row] = offlineSessionsFromReplica([rec('a', { profile: 'work' })], connFor)

    expect(row).toMatchObject({
      connection_id: 'conn-mini',
      id: 'a',
      is_active: false,
      offline_cached: true,
      offline_gateway: 'mini',
      offline_synced_at: 9_000,
      profile: 'work',
      title: 'Chat a'
    })
  })

  it("orders by the session's own activity, never the sync time", () => {
    const [row] = offlineSessionsFromReplica([rec('a', { activity: 5_000, synced_at: 99_999 })], connFor)

    expect(row.last_active).toBe(5_000)
  })

  it('never claims a stale running turn as current liveness', () => {
    const [row] = offlineSessionsFromReplica([rec('a', { running: true })], connFor)

    expect(row.is_active).toBe(false)
  })

  // [daveey] instant loads: an owner that published within OWNER_ASLEEP_AFTER_S is awake (hermes-hub watch keeps
  // the replica within seconds), so its rows are ordinary rows and show its running turns.
  it('an awake owner gives ordinary rows that show its running turn', () => {
    const now = 20_000
    const [row] = offlineSessionsFromReplica([rec('a', { running: true, synced_at: now - 5 })], connFor, now)

    expect(row.offline_cached).toBe(false)
    expect(row.is_active).toBe(true)
  })

  it('an owner silent past the threshold, or a stale replica, is asleep: cached and never running', () => {
    const now = 20_000
    const old = offlineSessionsFromReplica([rec('a', { running: true, synced_at: now - 181 })], connFor, now)[0]
    const edge = offlineSessionsFromReplica([rec('b', { running: true, synced_at: now - 180 })], connFor, now)[0]
    const stale = offlineSessionsFromReplica(
      [rec('c', { running: true, stale: true, synced_at: now })],
      connFor,
      now
    )[0]
    const never = offlineSessionsFromReplica([rec('d', { running: true, synced_at: null })], connFor, now)[0]

    expect([old.offline_cached, old.is_active]).toEqual([true, false])
    expect([edge.offline_cached, edge.is_active]).toEqual([false, true])
    expect([stale.offline_cached, stale.is_active]).toEqual([true, false])
    expect([never.offline_cached, never.is_active]).toEqual([true, false])
  })

  it('skips sources the normal recents list excludes (cron, subagent, api_server, messaging…)', () => {
    const rows = offlineSessionsFromReplica(
      ['cron', 'subagent', 'api_server', 'telegram', 'kanban', 'oneshot', 'acp', 'tool'].map(source =>
        rec(`s-${source}`, { source })
      ),
      connFor
    )

    expect(rows).toEqual([])
  })

  it('skips archived, hidden, local, empty, internal-child and untitled rows (no raw ids as titles)', () => {
    const rows = offlineSessionsFromReplica(
      [
        rec('arch', { archived: true }),
        rec('hid', { hidden: true }),
        rec('loc', { local: true }),
        rec('empty', { message_count: 0 }),
        rec('untitled', { title: '' }),
        rec('ok')
      ],
      connFor
    )

    expect(ids(rows)).toEqual(['ok'])
  })

  it('skips delegate_task children the owner stamped is_internal_child, whatever their source', () => {
    // A delegate child keeps its parent's source ('desktop'), so the source rule
    // let "Subagent: …" rows into Home; the owner's collector stamps the flag.
    const records = [
      rec('child', { is_internal_child: true, source: 'desktop', title: 'Subagent: do a thing' }),
      rec('parent', { is_internal_child: false })
    ]

    expect(ids(offlineSessionsFromReplica(records, connFor))).toEqual(['parent'])
  })

  it('keeps delegate children out of the boot snapshot of this gateway too', () => {
    const records = [rec('child', { is_internal_child: true, local: true }), rec('mine', { local: true })]

    expect(ids(offlineSessionsFromReplica(records, connFor, undefined, { includeLocal: true }))).toEqual(['mine'])
  })

  it('skips gateways this Desktop has no connection for (gateway selection needs a registry id)', () => {
    expect(offlineSessionsFromReplica([rec('x', { gateway: 'ghost' })], connFor)).toEqual([])
  })

  it('collapses a compression chain to its tip and records the lineage', () => {
    const rows = offlineSessionsFromReplica(
      [
        rec('root', { end_reason: 'compression' }),
        rec('mid', { end_reason: 'compression', parent_session_id: 'root' }),
        rec('tip', { parent_session_id: 'mid' })
      ],
      connFor
    )

    expect(ids(rows)).toEqual(['tip'])
    expect(rows[0]._lineage_root_id).toBe('root')
    expect(rows[0]._lineage_ids).toEqual(['root', 'mid', 'tip'])
  })

  it('carries the pinned flag and an unstamped owner policy counts as automated (never guessed visible)', () => {
    const [pinned] = offlineSessionsFromReplica([rec('p', { pinned: true })], connFor)
    const [unstamped] = offlineSessionsFromReplica([rec('u', { automated: undefined })], connFor)

    expect(pinned.pinned).toBe(true)
    expect(unstamped.automated).toBe(true)
  })
})

describe('normal sidebar predicates apply to cached rows', () => {
  const cached = offlineSessionsFromReplica(
    [rec('user'), rec('bot', { automated: true }), rec('bot-pinned', { automated: true, pinned: true })],
    connFor
  )

  it('automated/test rows are hidden with the toggle off and shown with it on', () => {
    const visible = (show: boolean) =>
      cached.filter(row => !sessionHiddenAsAutomated(row, show, { dotState: 'idle', pinnedIds: [] })).map(r => r.id)

    expect(visible(false)).toEqual(['user', 'bot-pinned'])
    expect(visible(true)).toEqual(['user', 'bot', 'bot-pinned'])
  })

  it('honours profile selection', () => {
    const rows = offlineSessionsFromReplica([rec('d'), rec('w', { profile: 'work' })], connFor)

    expect(ids(filterSessionsByProfileScope(rows, 'work'))).toEqual(['w'])
    expect(ids(filterSessionsByProfileScope(rows, 'default'))).toEqual(['d'])
  })
})

describe('mergeOfflineFallback', () => {
  const cached = offlineSessionsFromReplica([rec('c1'), rec('c2', { profile: 'work' })], connFor)

  it('adds cached rows when the gateway has no live rows', () => {
    expect(ids(mergeOfflineFallback([live('l1')], cached))).toEqual(['l1', 'c1', 'c2'])
  })

  it('live wins only for its owning connection and profile (reconnect, no duplicates)', () => {
    const merged = mergeOfflineFallback([live('l1'), live('c1', { connection_id: 'conn-mini' })], cached)

    expect(ids(merged)).toEqual(['l1', 'c1', 'c2'])
    expect(merged.find(r => r.id === 'c1')?.offline_cached).toBeUndefined()
  })

  it('same id on another gateway or profile is a different session', () => {
    const merged = mergeOfflineFallback([live('c1', { connection_id: 'conn-cloud' })], cached)

    expect(merged.filter(r => r.id === 'c1')).toHaveLength(2)
  })

  it('a live lineage id also dedupes the cached row', () => {
    const merged = mergeOfflineFallback(
      [live('tip', { _lineage_ids: ['c2', 'tip'], connection_id: 'conn-x', profile: 'work' })],
      [{ ...cached[1], connection_id: 'conn-x' }]
    )

    expect(ids(merged)).toEqual(['tip'])
  })

  it('a retained row of a failed list is stamped stale-list only when no replica copy replaces it', () => {
    recordSessionListResult([], [], 'conn-mini', 'all', [], true)

    const merged = mergeOfflineFallback(
      [live('c1', { connection_id: 'conn-mini' }), live('busy', { connection_id: 'conn-mini' })],
      cached
    )

    expect(merged.find(r => r.id === 'busy')).toMatchObject({
      is_active: false,
      offline_cached: true,
      offline_stale_list: true
    })
    expect(merged.find(r => r.id === 'c1')).toMatchObject({ offline_cached: true, offline_gateway: 'mini' })
    expect(merged.find(r => r.id === 'c1')?.offline_stale_list).toBeUndefined()
  })

  it("a failed local list keeps this Mac's own rows stale-list, one row each, over their asleep boot copies", () => {
    recordSessionListResult([], [], undefined, 'all', [], true)

    const boot = offlineSessionsFromReplica(
      [rec('bare', { gateway: 'macbook', local: true }), rec('named', { gateway: 'macbook', local: true })],
      gateway => (gateway === 'macbook' ? 'local' : undefined),
      1_000_000,
      { includeLocal: true }
    )

    const merged = mergeOfflineFallback([live('bare'), live('named', { connection_id: 'local' })], boot)

    expect(merged.map(r => [r.id, r.offline_stale_list])).toEqual([
      ['bare', true],
      ['named', true]
    ])
  })

  // [daveey] Boot-snapshot copies of this Mac's own chats: no activity stamps (cron_active, kanban_*).
  const ownBoot = () =>
    offlineSessionsFromReplica(
      [rec('older', { gateway: 'macbook', local: true }), rec('recent', { gateway: 'macbook', local: true })],
      gateway => (gateway === 'macbook' ? 'local' : undefined),
      9_000,
      { includeLocal: true }
    )

  it("a failed list on another gateway leaves this Mac's own rows live and adds no boot copies", () => {
    const previous = [
      live('recent', { connection_id: 'local', profile: 'default' }),
      live('h1', { connection_id: 'hermes-hub', profile: 'default' })
    ]

    recordSessionListResult(previous, [], 'hermes-hub', 'default', [], true)
    const merged = mergeOfflineFallback(previous, ownBoot())

    expect(merged.find(r => r.id === 'recent')?.offline_cached).toBeUndefined()
    expect(merged.find(r => r.id === 'h1')).toMatchObject({ offline_cached: true, offline_stale_list: true })
    expect(ids(merged)).toEqual(['recent', 'h1'])
  })

  it('a backend error without connection_id belongs to the gateway the list was sent to', () => {
    const previous = [
      live('recent', { connection_id: 'local', profile: 'default' }),
      live('h1', { connection_id: 'hermes-hub', profile: 'default' })
    ]

    recordSessionListResult(previous, [], 'hermes-hub', 'default', [{ profile: 'default' }])
    const merged = mergeOfflineFallback(previous, [])

    expect(merged.find(r => r.id === 'recent')?.offline_cached).toBeUndefined()
    expect(merged.find(r => r.id === 'h1')?.offline_cached).toBe(true)
  })

  it("once this Mac's backend answered, a failed local list never re-adds its stamp-less boot copies", () => {
    recordSessionListResult([], [], undefined, 'default', [], true)
    const merged = mergeOfflineFallback([live('recent', { connection_id: 'local' })], ownBoot())

    expect(ids(merged)).toEqual(['recent'])
    expect(merged[0]).toMatchObject({ offline_stale_list: true })
  })

  it('on a cold start (no live row of this Mac yet) the boot copies of its chats still paint', () => {
    recordSessionListResult([], [], undefined, 'default', [], true)

    expect(ids(mergeOfflineFallback([], ownBoot())).sort()).toEqual(['older', 'recent'])
    expect(ids(mergeOfflineFallback([live('h1', { connection_id: 'hermes-hub' })], ownBoot())).sort()).toEqual([
      'h1',
      'older',
      'recent'
    ])
  })

  it('an error naming another gateway still marks that gateway stale', () => {
    const previous = [live('mine', { connection_id: 'local' }), live('hubrow', { connection_id: 'conn-hub' })]

    recordSessionListResult(previous, [], 'conn-hub', 'all', [{ connection_id: 'local', profile: 'default' }], true)

    expect(sessionOwnerIsFresh('local', 'default')).toBe(false)
    expect(sessionOwnerIsFresh('conn-hub', 'default')).toBe(false)
  })

  it('keeps reference identity when nothing is added', () => {
    const rows = [live('l1')]

    expect(mergeOfflineFallback(rows, [])).toBe(rows)
  })

  // [daveey] Active gateway = the hub, so its list holds no row of this Mac: this Mac's older chats come in as awake
  // replica rows (no activity stamps) while the projects tree carries the backend's stamped copy. The overlay lets
  // the merged row beat the tree's, so the replica copy blanked the grey waiting stripe of parked work.
  describe('an awake owner whose backend reported the session in its projects tree', () => {
    const awake = () =>
      offlineSessionsFromReplica(
        [rec('parked', { gateway: 'macbook' }), rec('untreed', { gateway: 'macbook' })],
        gateway => (gateway === 'macbook' ? 'local' : undefined),
        9_000
      )

    const stamped = live('parked', {
      connection_id: 'local',
      kanban_active: 1,
      kanban_observed_at: 9_000,
      kanban_parked: 1,
      profile: 'default'
    })

    const hubList = [live('h1', { connection_id: 'hermes-hub' })]

    it('uses the stamped tree row instead of the replica copy', () => {
      const merged = mergeOfflineFallback(hubList, awake(), undefined, [stamped])

      expect(ids(merged)).toEqual(['h1', 'parked', 'untreed'])
      expect(merged.find(r => r.id === 'parked')).toBe(stamped)
      // Control: a session the tree did not report keeps its replica row.
      expect(merged.find(r => r.id === 'untreed')?.offline_synced_at).toBe(9_000)
    })

    it('without tree rows the replica copy is used, unchanged (the old behaviour)', () => {
      const merged = mergeOfflineFallback(hubList, awake())

      expect(merged.find(r => r.id === 'parked')).toMatchObject({ offline_synced_at: 9_000 })
      expect(merged.find(r => r.id === 'parked')?.kanban_parked).toBeUndefined()
    })

    it('a tree row of another gateway or profile, or a replica-derived tree row, does not replace it', () => {
      const merged = mergeOfflineFallback(hubList, awake(), undefined, [
        { ...stamped, connection_id: 'hermes-hub' },
        { ...stamped, profile: 'work' },
        { ...stamped, offline_synced_at: 8_000 }
      ])

      expect(merged.find(r => r.id === 'parked')?.kanban_parked).toBeUndefined()
    })

    it("an asleep owner's cached row stays cached (opens read-only), even with a tree copy", () => {
      const asleep = offlineSessionsFromReplica(
        [rec('parked', { gateway: 'macbook', synced_at: 1_000 })],
        gateway => (gateway === 'macbook' ? 'local' : undefined),
        9_000
      )

      const merged = mergeOfflineFallback(hubList, asleep, undefined, [stamped])

      expect(merged.find(r => r.id === 'parked')).toMatchObject({ offline_cached: true })
    })
  })

  // [daveey] The tree carries only a few previews per project, so most of this Mac's older chats have no tree copy
  // (20261026_170127_f07c32: 5 cards, all parked, painted no stripe). Their owner's stamps come from
  // /api/profiles/sessions/stamps (owner-row-stamps.ts) and are folded into the replica row.
  describe('an awake owner whose session the projects tree does not carry', () => {
    const hubList = [live('h1', { connection_id: 'hermes-hub' })]
    const toLocal = (gateway: string) => (gateway === 'macbook' ? 'local' : undefined)

    beforeEach(() => _resetOwnerRowStampsForTests())

    it("gets its owner's kanban stamps and paints the grey waiting stripe; an asleep row is unchanged", async () => {
      const replica = offlineSessionsFromReplica(
        [
          rec('f07c32', { gateway: 'macbook' }),
          rec('asleep', { gateway: 'macbook', profile: 'work', synced_at: 1_000 })
        ],
        toLocal,
        9_000
      )

      const asked: [string, string, string[]][] = []

      const fetcher: StampFetcher = async (connectionId, profile, wanted) => {
        asked.push([connectionId, profile, wanted])

        return wanted.map(id => ({
          cwd: '/Users/daveey/code/infra',
          id,
          kanban_active: 5,
          kanban_blocked: 0,
          kanban_observed_at: 9_000,
          kanban_parked: 5,
          profile
        }))
      }

      const before = mergeOfflineFallback(hubList, replica, undefined, [])
      expect(kanbanStripeCounts(before.find(r => r.id === 'f07c32')!).waiting).toBe(0)

      await refreshOwnerRowStamps(before, fetcher, 50_000)
      // Only the awake row is asked for; the asleep (offline_cached) row's owner is unreachable.
      expect(asked).toEqual([['local', 'default', ['f07c32']]])

      const merged = mergeOfflineFallback(hubList, replica, undefined, [], $ownerRowStamps.get())
      const row = merged.find(r => r.id === 'f07c32')!

      expect(row).toMatchObject({
        connection_id: 'local',
        cwd: '/Users/daveey/code/infra',
        kanban_active: 5,
        kanban_parked: 5,
        offline_synced_at: 9_000,
        title: 'Chat f07c32'
      })
      expect(row.offline_cached).toBe(false)
      expect(kanbanStripeCounts(row)).toEqual({
        blocked: 0,
        external: 0,
        externalDown: 0,
        externalUnknown: 0,
        stalled: 0,
        waiting: 5
      })

      // Control: the asleep owner's cached row is the very same row as before (no stamps, still cached).
      const asleepRow = merged.find(r => r.id === 'asleep')!
      expect(asleepRow).toBe(before.find(r => r.id === 'asleep'))
      expect(asleepRow).toMatchObject({ offline_cached: true })
      expect(asleepRow.kanban_parked).toBeUndefined()
    })

    it("a stamp keyed to an asleep row's identity is never applied to it", () => {
      const asleep = offlineSessionsFromReplica([rec('x', { gateway: 'macbook', synced_at: 1_000 })], toLocal, 9_000)
      const stamps = new Map([[`local\u0000default\u0000x`, { kanban_active: 2, kanban_parked: 2 }]])
      const merged = mergeOfflineFallback(hubList, asleep, undefined, [], stamps)

      expect(merged.find(r => r.id === 'x')).toBe(asleep[0])
    })

    it('a tree copy still wins over owner stamps', () => {
      const replica = offlineSessionsFromReplica([rec('t', { gateway: 'macbook' })], toLocal, 9_000)
      const tree = live('t', { connection_id: 'local', kanban_active: 1, kanban_parked: 1, profile: 'default' })
      const stamps = new Map([[`local\u0000default\u0000t`, { kanban_active: 9 }]])

      expect(mergeOfflineFallback(hubList, replica, undefined, [tree], stamps).find(r => r.id === 't')).toBe(tree)
    })
  })
})

describe('syncedAgoLabel', () => {
  it('describes the sync age', () => {
    expect(syncedAgoLabel(null)).toBe('never synced')
    expect(syncedAgoLabel(1_000, 1_030_000)).toBe('synced just now')
    expect(syncedAgoLabel(1_000, 1_000_000 + 600_000)).toBe('synced 10m ago')
  })
})

it('uses ordinary gateway visibility, owner grouping, and pin resolution for cached rows', () => {
  const cached = offlineSessionsFromReplica(
    [rec('pin', { pinned: true }), rec('work', { profile: 'work' }), rec('cloud', { gateway: 'cloud' })],
    connFor
  )

  const shown = cached.filter(row => !sessionOnHiddenGateway(row, ['conn-cloud'], null))
  expect(ids(shown)).toEqual(['pin', 'work'])
  const groups = buildGatewaySessionGroups(shown, null, {})
  expect(groups.map(group => [group.connectionId, group.profile, ids(group.sessions)])).toEqual([
    ['conn-mini', 'default', ['pin']],
    ['conn-mini', 'work', ['work']]
  ])
  expect(ids(resolvePinnedSessions([], buildSessionByAnyId(shown, [], []), shown, new Set()))).toEqual(['pin'])
})

it('freshness and retained-row archive hiding stay scoped to the full owner', () => {
  const cached = offlineSessionsFromReplica(
    [rec('same'), rec('same', { profile: 'work' }), rec('same', { gateway: 'cloud' })],
    connFor
  )

  const previous = cached.map(row => ({ ...row, offline_cached: false, is_active: true }))
  const incoming = previous.filter(row => row.connection_id === 'conn-cloud')
  recordSessionListResult(previous, incoming, 'primary', 'all', [{ connection_id: 'conn-mini', profile: 'work' }])
  const merged = mergeOfflineFallback(previous, cached)
  expect(merged.filter(row => row.offline_cached)).toEqual([cached[1]])
  expect(merged.filter(row => row.is_active)).toHaveLength(2)
  hideOfflineSession(cached[1])
  expect(mergeOfflineFallback(previous, cached)).toEqual([previous[0], previous[2]])
})

describe('gateway-unavailable probe', () => {
  beforeEach(() => _resetOfflineSessionsForTests())

  // [daveey] Probe invalidation must also make the owner eligible for a recovery list.
  it('leaves the gateway a stale owner so the backoff re-list runs until a list succeeds', () => {
    recordSessionGatewayUnavailable('hermes-hub')
    expect(sessionOwnerIsFresh('hermes-hub', 'default')).toBe(false)
    expect(staleSessionOwners()).toEqual(['hermes-hub/*'])

    recordSessionListResult(
      [],
      [{ id: 's1', profile: 'default', connection_id: 'hermes-hub' } as SessionInfo],
      'hermes-hub',
      'all'
    )
    expect(sessionOwnerIsFresh('hermes-hub', 'default')).toBe(true)
    expect(staleSessionOwners()).toEqual([])
  })
})
