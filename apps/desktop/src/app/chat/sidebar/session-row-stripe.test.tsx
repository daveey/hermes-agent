import { cleanup, render, renderHook } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { $queuedPromptsBySession } from '@/store/composer-queue'
import { reconcilePendingSubmissions } from '@/store/pending-submissions'
import { $sessions, setSessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { SessionRowStatusStripe, useSessionRowStripe } from './session-row-stripe'

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

// [daveey] A chat with an open decision whose linked card is running (dot state `background`) paints red from its
// outcome; the stripe's tooltip must name the question then too, not go blank because "something is running".
it('a red decision stripe on a chat whose card is running still names the question', () => {
  const session = row('decision-chat', {
    kanban_active: 1,
    outcome: {
      at: 1,
      items: [{ choices: [], id: 'req-1', question: 'Build A, B and C?', state: 'open' }],
      kind: 'decision',
      source: 'derived'
    }
  } as Partial<SessionInfo>)

  setSessions([session])

  const { result } = renderHook(() => useSessionRowStripe(session, 'background', true))

  expect(result.current.statusStripe).toBe('stuck')
  expect(result.current.outcomePainted).toBe(true)

  const { container } = render(
    <SessionRowStatusStripe
      outcomePainted={result.current.outcomePainted}
      session={session}
      statusStripe={result.current.statusStripe}
    />
  )

  expect(stripe(container)?.getAttribute('aria-label')).toBe('Build A, B and C?')
})

it('the chat\'s own live turn still beats the outcome, for the stripe and its tooltip alike', () => {
  const session = row('working-chat', {
    outcome: { at: 1, items: [{ choices: [], id: 'req-2', question: 'Q?', state: 'open' }], kind: 'decision', source: 'derived' }
  } as Partial<SessionInfo>)

  setSessions([session])

  const { result } = renderHook(() => useSessionRowStripe(session, 'working', true))

  expect(result.current.outcomePainted).toBe(false)
  expect(result.current.statusStripe).not.toBe('stuck')
})
