import { cleanup, render, screen } from '@testing-library/react'
import type * as React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { $sidebarShowAllSessions, resetSidebarView } from '@/store/layout'
import { $sessions } from '@/store/session'

import { SidebarSessionsSection } from './sessions-section'

vi.mock('./session-row', () => ({
  SidebarSessionRow: ({ session, onArchive }: { session: SessionInfo; onArchive: () => void }) => (
    <div data-testid="session-row" onClick={onArchive}>
      {session.id}
    </div>
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
        projects: {
          toggle: (label: string, open: boolean) => `${open ? 'Hide' : 'Show'} ${label}`
        },
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

afterEach(cleanup)

beforeEach(() => {
  resetSidebarView()
  $sidebarShowAllSessions.set(true)
  $sessions.set([])
})

vi.mock('./virtual-session-list', () => ({
  VirtualSessionList: () => <div>VIRTUAL FLAT</div>
}))

const emptyProject = {
  id: 'primary',
  label: 'app',
  path: null,
  repos: [],
  sessionCount: 0,
  sessionIds: []
}

function entered(props: Partial<React.ComponentProps<typeof SidebarSessionsSection>> = {}) {
  return render(
    <SidebarSessionsSection
      activeSessionId={null}
      emptyState={<span>EMPTY</span>}
      label="Projects"
      onArchiveSession={noop}
      onDeleteSession={noop}
      onResumeSession={noop}
      onToggle={noop}
      onTogglePin={noop}
      onToggleUnread={noop}
      open
      pinned={false}
      projectBackRow={<span>BACK</span>}
      sessions={[]}
      {...props}
    />
  )
}

it.each([
  ['without primary content', {}],
  ['with an empty primary', { projectContent: emptyProject }],
  ['while primary tree loads', { projectsLoading: true }],
  [
    'with a long stale recents list',
    {
      sessions: Array.from({ length: 30 }, (_, i) => session(`stale-${i}`, 0, 0))
    }
  ]
])('keeps grouped remote body visible %s', (_label, props) => {
  entered({ ...props, projectMemberBody: () => <span>REMOTE BODY</span> })
  expect(screen.getAllByText('REMOTE BODY')).toHaveLength(1)
  expect(screen.getByText('BACK')).toBeTruthy()
  expect(screen.queryByText('EMPTY')).toBeNull()
  expect(screen.queryByText('VIRTUAL FLAT')).toBeNull()
})
it('keeps singleton empty-state path and back row unchanged', () => {
  entered({ projectContent: emptyProject })
  expect(screen.getByText('EMPTY')).toBeTruthy()
  expect(screen.getByText('BACK')).toBeTruthy()
  expect(screen.queryByText('REMOTE BODY')).toBeNull()
})
it('still virtualizes flat recents without entered members', () => {
  entered({
    sessions: Array.from({ length: 30 }, (_, i) => session(`flat-${i}`, 0, 0))
  })
  expect(screen.getByText('VIRTUAL FLAT')).toBeTruthy()
})
