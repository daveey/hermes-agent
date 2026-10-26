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
  recordSessionListResult,
  syncedAgoLabel
} from './offline-sessions'

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

  it('keeps reference identity when nothing is added', () => {
    const rows = [live('l1')]

    expect(mergeOfflineFallback(rows, [])).toBe(rows)
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
  const cached = offlineSessionsFromReplica([
    rec('same'), rec('same', { profile: 'work' }), rec('same', { gateway: 'cloud' })
  ], connFor)

  const previous = cached.map(row => ({ ...row, offline_cached: false, is_active: true }))
  const incoming = previous.filter(row => row.connection_id === 'conn-cloud')
  recordSessionListResult(previous, incoming, 'primary', 'all', [{ connection_id: 'conn-mini', profile: 'work' }])
  const merged = mergeOfflineFallback(previous, cached)
  expect(merged.filter(row => row.offline_cached)).toEqual([cached[1]])
  expect(merged.filter(row => row.is_active)).toHaveLength(2)
  hideOfflineSession(cached[1])
  expect(mergeOfflineFallback(previous, cached)).toEqual([previous[0], previous[2]])
})
