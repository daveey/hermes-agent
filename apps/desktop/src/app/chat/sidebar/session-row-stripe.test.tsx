import { cleanup, render } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { $queuedPromptsBySession } from '@/store/composer-queue'
import { reconcilePendingSubmissions } from '@/store/pending-submissions'
import { $sessions, setSessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { SessionRowStatusStripe } from './session-row-stripe'

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 4, source: 'desktop', started_at: 0, title: id, ...extra }) as SessionInfo

const stripe = (container: HTMLElement) => container.querySelector('[data-slot="session-status-stripe"]')

afterEach(() => {
  cleanup()
  window.localStorage.clear()
  $queuedPromptsBySession.set({})
  $sessions.set([])
})

// [daveey] The red of a queue card a restart may have lost is this app's own: the backend's status shows nothing,
// so the stripe says what it is and what the user can do about it.
it('a red painted by a queue card a restart may have lost names its cause', () => {
  setSessions([row('hub-chat')])
  reconcilePendingSubmissions('hub-chat', [{ admission_id: 'lost-turn', status: 'unknown', user: 'resume' }])

  const { container } = render(
    <SessionRowStatusStripe outcomePainted={false} session={row('hub-chat')} statusStripe="stuck" />
  )

  expect(stripe(container)?.getAttribute('aria-label')).toBe(
    'A message queued before a restart may not have run: Send again or Discard'
  )
})

it('a red with no paused queue card keeps its own explanation', () => {
  setSessions([row('other')])

  const { container } = render(
    <SessionRowStatusStripe outcomePainted={false} session={row('other')} statusStripe="stuck" />
  )

  expect(stripe(container)?.getAttribute('aria-label')).toBeNull()
})
