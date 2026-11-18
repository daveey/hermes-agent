import fs from 'node:fs'

import { describe, expect, it } from 'vitest'

import { markInboxSessions } from './transcript-cache-host'
import { type HubSessionRecord, MIN_REFETCH_MS, needsRefresh, selectPrefetchTargets } from './transcript-prefetch'

const fixture: {
  full: { available: boolean; sessions: HubSessionRecord[] }
  inbox: Parameters<typeof markInboxSessions>[1]
  prefetch_fields: string[]
} = JSON.parse(
  fs.readFileSync(new URL('./fixtures/hub-sync-sessions-prefetch-projection.json', import.meta.url), 'utf8')
)

// Fixed server contract: copy only existing fields; Inbox is injected by the host, never projected.
const fields = [
  'activity',
  'archived',
  'automated',
  'gateway',
  'hidden',
  'id',
  'message_count',
  'parent_session_id',
  'profile',
  'started_at'
]

const full: HubSessionRecord[] = fixture.full.sessions

const projected = full.map(record =>
  Object.fromEntries(Object.entries(record).filter(([key]) => fields.includes(key)))
) as HubSessionRecord[]

const connections: Record<string, string> = { mini: 'mac-mini', hub: 'remote-hub' }
const connectionFor = (gateway: string) => connections[gateway]

const targetsFor = (records: HubSessionRecord[], limit: number) =>
  selectPrefetchTargets(markInboxSessions(records, fixture.inbox), connectionFor, limit)

describe('fixed prefetch projection compatibility', () => {
  it('preserves missing and null fields without projecting caller-owned Inbox flags', () => {
    expect(fields).toEqual(fixture.prefetch_fields)
    expect(projected.every(record => !Object.hasOwn(record, 'inbox'))).toBe(true)
    expect(projected.find(record => record.id === 'missing-profile')).not.toHaveProperty('profile')
    expect(projected.find(record => record.id === 'null-profile')).toHaveProperty('profile', null)
    expect(projected.find(record => record.id === 'missing-activity')).not.toHaveProperty('activity')
    expect(projected.find(record => record.id === 'missing-count')).not.toHaveProperty('message_count')
  })

  it.each([1, 2, 5, 200])('preserves ordered targets and refresh decisions with %i slots per gateway', limit => {
    const original = targetsFor(full, limit)
    const reduced = targetsFor(projected, limit)
    expect(original.length).toBeGreaterThan(0)
    expect(reduced).toEqual(original)

    original.forEach((target, index) => {
      const now = 100_000

      const cachedStates = [
        null,
        { ...target.meta, savedAt: 0 },
        { ...target.meta, activity: (target.meta.activity ?? 0) + 1, savedAt: now },
        { ...target.meta, activity: (target.meta.activity ?? 0) + 1, savedAt: now - MIN_REFETCH_MS },
        { ...target.meta, messageCount: (target.meta.messageCount ?? 0) + 1, savedAt: now - MIN_REFETCH_MS + 1 },
        { ...target.meta, messageCount: (target.meta.messageCount ?? 0) + 1, savedAt: now - MIN_REFETCH_MS }
      ]

      const decisions = cachedStates.map(cached => needsRefresh(cached, target, now))
      expect(decisions).toEqual([true, false, false, true, false, true])
      expect(cachedStates.map(cached => needsRefresh(cached, reduced[index], now))).toEqual(decisions)
    })
  })

  it('exercises compression scope, stable ties, profile normalization and Inbox priority', () => {
    const targets = targetsFor(projected, 200)
    const ids = targets.map(target => target.key.sessionId)
    expect(ids).not.toContain('root')
    expect(ids).toContain('tip')
    expect(ids).toContain('other-root') // other-tip has a different profile: not its compression child
    expect(ids).not.toContain('secret')
    expect(ids).not.toContain('gone')
    expect(ids).not.toContain('x') // unregistered gateway
    expect(ids.indexOf('tie-a')).toBeLessThan(ids.indexOf('tie-b'))
    expect(ids.filter(id => id === 'shared-id')).toHaveLength(2)

    for (const id of ['missing-profile', 'null-profile', 'empty-profile']) {
      expect(targets.find(target => target.key.sessionId === id)?.key.profile).toBe('default')
    }

    expect(targets.find(target => target.key.sessionId === 'space-profile')?.key.profile).toBe('work')
    expect(targets.filter(target => target.inbox).map(target => target.key.sessionId)).toEqual([
      'listed-chat',
      'archived-card',
      'card'
    ])
    expect(targets.slice(0, 3).every(target => target.inbox)).toBe(true)
    expect(targets.find(target => target.key.sessionId === 'card')?.automated).toBe(true)
    expect(targetsFor(projected, 3).some(target => target.key.sessionId === 'null-activity')).toBe(true)
    expect(targetsFor(projected, 3).some(target => target.key.sessionId === 'missing-activity')).toBe(true)
  })

  it('removes replica display payload while preserving the response envelope', () => {
    const response = { ...fixture.full, sessions: projected }
    const fullBytes = Buffer.byteLength(JSON.stringify(fixture.full))
    const projectedBytes = Buffer.byteLength(JSON.stringify(response))
    expect(response.available).toBe(true)
    expect(projectedBytes).toBeLessThan(fullBytes / 2)
    console.info(
      JSON.stringify({
        fixtureRows: full.length,
        fullBytes,
        projectedBytes,
        targets: [1, 2, 5, 200].map(limit => ({ limit, count: targetsFor(projected, limit).length }))
      })
    )
  })
})
