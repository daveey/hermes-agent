import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { $sidebarShowAllSessions, resetSidebarView, setSidebarOrdering } from '@/store/layout'
import { $sessions } from '@/store/session'
import { $sidebarSessionRankIds } from '@/store/sidebar-sort'

import { SidebarSessionsSection } from './sessions-section'

vi.mock('./session-row', () => ({
  SidebarSessionRow: ({ session }: { session: SessionInfo }) => (
    <div data-testid="session-row">{session.id}</div>
  )
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { cancel: 'Cancel' },
      desktop: {},
      sidebar: {
        dateDivider: {
          earlier: 'Earlier',
          earlierThisMonth: 'Earlier this month',
          lastMonth: 'Last month',
          lastWeek: 'Last week',
          older: 'Older',
          thisMonth: 'This month',
          thisWeek: 'This week',
          today: 'Today',
          yesterday: 'Yesterday'
        },
        nav: { 'new-session': 'New session' },
        noSessions: 'No sessions yet',
        projects: { toggle: (label: string, open: boolean) => `${open ? 'Hide' : 'Show'} ${label}` },
        statusDivider: { done: 'Done', working: 'Working' }
      }
    }
  })
}))

const noop = vi.fn()
const now = Math.floor(Date.now() / 1000)
const DAY = 24 * 60 * 60

const session = (id: string, startedAgo: number, activeAgo: number) =>
  ({
    handoff_platform: null,
    handoff_state: null,
    id,
    input_tokens: 0,
    last_active: now - activeAgo,
    output_tokens: 0,
    profile: 'default',
    started_at: now - startedAgo
  }) as unknown as SessionInfo

// What the sidebar index hands the section: rows newest-activity first.
const byActivity = (rows: SessionInfo[]) => [...rows].sort((a, b) => (b.last_active ?? 0) - (a.last_active ?? 0))

function renderFlat(rows: SessionInfo[]) {
  $sessions.set(rows)

  return render(
    <SidebarSessionsSection
      activeSessionId={null}
      emptyState={null}
      grouping="date"
      label="Sessions"
      manualOrderIds={$sidebarSessionRankIds.get()}
      onArchiveSession={noop}
      onDeleteSession={noop}
      onResumeSession={noop}
      onToggle={noop}
      onTogglePin={noop}
      onToggleUnread={noop}
      open
      pinned={false}
      sessions={byActivity(rows)}
    />
  )
}

const renderedIds = () => screen.getAllByTestId('session-row').map(row => row.textContent)

afterEach(cleanup)

beforeEach(() => {
  resetSidebarView()
  $sidebarShowAllSessions.set(true)
  $sessions.set([])
})

describe('flat recents sorted by creation time', () => {
  // Created two weeks ago, then a week ago, then today — but the oldest one is
  // the chat in use right now, and the middle one was touched this morning.
  const rows = () => [session('old', 14 * DAY, 60), session('mid', 7 * DAY, 3 * 60 * 60), session('new', 2 * 60 * 60, 2 * 60 * 60)]

  it('orders rows by started_at, not by last activity', () => {
    setSidebarOrdering('created')
    renderFlat(rows())

    expect(renderedIds()).toEqual(['new', 'mid', 'old'])
  })

  it('does not move a row when an older session gets new activity', () => {
    setSidebarOrdering('created')
    const before = rows()
    const { unmount } = renderFlat(before)
    const first = renderedIds()

    unmount()

    // `mid` gets a reply: only last_active changes.
    renderFlat(before.map(row => (row.id === 'mid' ? { ...row, last_active: now } : row)))

    expect(renderedIds()).toEqual(first)
  })

  it('still orders by activity under the default "updated" sort', () => {
    renderFlat(rows())

    expect(renderedIds()).toEqual(['old', 'new', 'mid'])
  })
})
