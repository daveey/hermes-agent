import { afterEach, describe, expect, it } from 'vitest'

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
  it('hides automated rows when the toggle is off, never human or unstamped rows', () => {
    expect(sessionHiddenAsAutomated(bot, false, { dotState: 'idle' })).toBe(true)
    expect(sessionHiddenAsAutomated(human, false, { dotState: 'idle' })).toBe(false)
    expect(sessionHiddenAsAutomated(legacy, false, { dotState: 'idle' })).toBe(false)
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

  it('still hides a working or finished automated row', () => {
    expect(sessionHiddenAsAutomated(bot, false, { dotState: 'working' })).toBe(true)
    expect(sessionHiddenAsAutomated(bot, false, { dotState: 'unread' })).toBe(true)
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
