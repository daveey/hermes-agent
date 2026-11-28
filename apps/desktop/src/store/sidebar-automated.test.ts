import { afterEach, describe, expect, it } from 'vitest'

import fixture from '../../../../schemas/fixtures/execution-observations-v1.json'

import { $pinnedSessionIds, $sidebarFiltersActive, $sidebarShowAutomated, resetSidebarView } from './layout'
import { $blockedSessionIds } from './session-blocked'
import { sessionHiddenAsAutomated } from './sidebar-automated'

const bot = { automated: true, id: 'bot', message_count: 3 }
const human = { automated: false, id: 'human', message_count: 3 }
const legacy = { id: 'legacy', message_count: 3 } // backend predating the stamp

afterEach(() => {
  $sidebarShowAutomated.set(false)
  $pinnedSessionIds.set([])
  $blockedSessionIds.set([])
})

describe('sessionHiddenAsAutomated [daveey]', () => {
  it('uses the caller render clock when expiring live evidence reveals a stalled parked card', () => {
    const row = {
      ...bot,
      kanban_active: 1,
      kanban_parked: 1,
      kanban_stalled: 1,
      execution_observations: [{ ...fixture.base, valid_until: 1010 }]
    }

    expect(sessionHiddenAsAutomated(row, false, { dotState: 'background', nowSeconds: 1001 })).toBe(true)
    expect(sessionHiddenAsAutomated(row, false, { dotState: 'background', nowSeconds: 1011 })).toBe(false)
  })

  it('hides automated rows when the toggle is off, never human or unstamped rows', () => {
    expect(sessionHiddenAsAutomated(bot, false, { dotState: 'idle' })).toBe(true)
    expect(sessionHiddenAsAutomated(human, false, { dotState: 'idle' })).toBe(false)
    expect(sessionHiddenAsAutomated(legacy, false, { dotState: 'idle' })).toBe(false)
  })

  it('resurfaces an automated row when external work is down, not when merely running', () => {
    const row = { ...bot, kanban_active: 1, kanban_parked: 1, kanban_observed_at: 1000 }
    const ctx = { dotState: 'idle' as const, nowSeconds: 1000 }

    expect(sessionHiddenAsAutomated({ ...row, kanban_external: 1 }, false, ctx)).toBe(true)
    expect(sessionHiddenAsAutomated({ ...row, kanban_external_down: 1 }, false, ctx)).toBe(false)
  })

  it('shows automated rows when the toggle is on', () => {
    expect(sessionHiddenAsAutomated(bot, true, { dotState: 'idle' })).toBe(false)
    $sidebarShowAutomated.set(true)
    expect(sessionHiddenAsAutomated(bot)).toBe(false)
  })

  it('keeps pinned automated rows (server flag, local pin, or lineage-root pin)', () => {
    expect(sessionHiddenAsAutomated({ ...bot, pinned: true }, false, { dotState: 'idle' })).toBe(false)
    expect(sessionHiddenAsAutomated(bot, false, { dotState: 'idle', pinnedIds: ['bot'] })).toBe(false)
    expect(
      sessionHiddenAsAutomated({ ...bot, _lineage_root_id: 'root' }, false, { dotState: 'idle', pinnedIds: ['root'] })
    ).toBe(false)
    $pinnedSessionIds.set(['bot'])
    expect(sessionHiddenAsAutomated(bot, false, { dotState: 'idle' })).toBe(false)
  })

  it('keeps automated rows that are blocked or need attention', () => {
    expect(sessionHiddenAsAutomated(bot, false, { dotState: 'needs-input' })).toBe(false)
    expect(sessionHiddenAsAutomated(bot, false, { dotState: 'stalled' })).toBe(false)
    expect(sessionHiddenAsAutomated({ ...bot, kanban_blocked: 1 }, false, { dotState: 'idle' })).toBe(false)
    expect(sessionHiddenAsAutomated({ ...bot, review_wait: 'stuck' }, false, { dotState: 'idle' })).toBe(false)
    expect(sessionHiddenAsAutomated({ ...bot, kanban_attention: 1 }, false, { dotState: 'idle' })).toBe(false)
    expect(sessionHiddenAsAutomated(bot, false, { dotState: 'idle', userBlocked: true })).toBe(false)
    $blockedSessionIds.set(['bot'])
    expect(sessionHiddenAsAutomated(bot, false, { dotState: 'idle' })).toBe(false)
  })

  it('hides automated rows waiting on cards unless they need human attention', () => {
    expect(sessionHiddenAsAutomated({ ...bot, kanban_active: 1 }, false, { dotState: 'idle' })).toBe(true)
    expect(sessionHiddenAsAutomated({ ...bot, kanban_active: 1, kanban_blocked: 1 }, false, { dotState: 'idle' })).toBe(
      false
    )
    expect(sessionHiddenAsAutomated({ ...bot, kanban_active: 0 }, false, { dotState: 'idle' })).toBe(true)
    expect(sessionHiddenAsAutomated(bot, false, { dotState: 'idle' })).toBe(true)
  })

  it('still hides a working or finished automated row', () => {
    expect(sessionHiddenAsAutomated(bot, false, { dotState: 'working' })).toBe(true)
    expect(sessionHiddenAsAutomated(bot, false, { dotState: 'unread' })).toBe(true)
  })
})

// [daveey] session-outcome: a stored outcome on bot / smoke-test traffic must not pull it back into the sidebar;
// only the signals automated rows always resurfaced on count.
describe('sessionHiddenAsAutomated ignores the stored outcome', () => {
  it.each(['unresolved', 'decision', 'done', 'waiting', 'running'])('a %s outcome keeps the row hidden', kind => {
    const row = { ...bot, outcome: { kind, reason: 'no_summary', items: [{ id: 'us_1', question: 'Merge?' }] } }

    expect(sessionHiddenAsAutomated(row as never, false, { dotState: 'idle' })).toBe(true)
  })

  it('the pre-existing signals still resurface it with an outcome present', () => {
    const row = { ...bot, kanban_attention: 1, outcome: { kind: 'done' } }

    expect(sessionHiddenAsAutomated(row as never, false, { dotState: 'idle' })).toBe(false)
    expect(
      sessionHiddenAsAutomated({ ...bot, outcome: { kind: 'done' } } as never, false, { dotState: 'needs-input' })
    ).toBe(false)
  })
})

describe('$sidebarShowAutomated', () => {
  it('defaults off without lighting the filter dot; on counts as a filter; reset turns it off', () => {
    expect($sidebarShowAutomated.get()).toBe(false)
    expect($sidebarFiltersActive.get()).toBe(false)
    $sidebarShowAutomated.set(true)
    expect($sidebarFiltersActive.get()).toBe(true)
    resetSidebarView()
    expect($sidebarShowAutomated.get()).toBe(false)
    expect($sidebarFiltersActive.get()).toBe(false)
  })
})
