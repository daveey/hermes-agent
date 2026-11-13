import { act, cleanup, render, renderHook } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $queuedPromptsBySession } from '@/store/composer-queue'
import { reconcilePendingSubmissions } from '@/store/pending-submissions'
import { $sessions, setSessions } from '@/store/session'
import { resetStripeTransitionLog, setStripeReasonReporter } from '@/store/session-dot-state'
import { clearAllSessionStates, publishSessionState } from '@/store/session-states'
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
  clearAllSessionStates()
  resetStripeTransitionLog()
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

it("the chat's own live turn still beats the outcome, for the stripe and its tooltip alike", () => {
  const session = row('working-chat', {
    outcome: {
      at: 1,
      items: [{ choices: [], id: 'req-2', question: 'Q?', state: 'open' }],
      kind: 'decision',
      source: 'derived'
    }
  } as Partial<SessionInfo>)

  setSessions([session])

  const { result } = renderHook(() => useSessionRowStripe(session, 'working', true))

  expect(result.current.outcomePainted).toBe(false)
  expect(result.current.statusStripe).not.toBe('stuck')
})

// [daveey] PR #1878 review: the red reasons of a `needs-input` row read $sessionStates / $pausedQueueSessionIds, which
// no row prop carries. When the prompt moves to another runtime the stripe and dot stay `stuck` / `needs-input`, and
// that new red must still be reported with its new runtime.
it('a red row whose prompt moved to another runtime is re-reported with the new runtime', () => {
  const reports: string[][] = []

  const prompt = (needsInput: boolean) => ({
    ...createClientSessionState(null),
    needsInput,
    storedSessionId: 'red-chat'
  })

  const session = row('red-chat')

  setSessions([session])
  setStripeReasonReporter((_row, _stripe, _previous, reasons) => {
    reports.push([...reasons])
  })

  try {
    publishSessionState('rt-a', prompt(true))
    renderHook(() => useSessionRowStripe(session, 'needs-input', true))
    expect(reports).toEqual([['needs-input:prompt runtime=rt-a']]) // first sighting of a red row

    act(() => {
      publishSessionState('rt-a', prompt(false))
      publishSessionState('rt-b', prompt(true))
    })

    expect(reports).toEqual([['needs-input:prompt runtime=rt-a'], ['needs-input:prompt runtime=rt-b']])
  } finally {
    setStripeReasonReporter(null)
  }
})

// [daveey] PR #1892 review: only a `needs-input` row reads the prompt inputs, so another runtime's prompt moving must
// not re-render every other sidebar row.
it('a row that is not needs-input does not re-render when a prompt moves', () => {
  const prompt = (needsInput: boolean) => ({
    ...createClientSessionState(null),
    needsInput,
    storedSessionId: 'red-chat'
  })

  const session = row('quiet-chat')
  let renders = 0

  setSessions([session, row('red-chat')])
  publishSessionState('rt-a', prompt(true))
  renderHook(() => {
    renders += 1

    return useSessionRowStripe(session, 'idle', true)
  })
  const before = renders

  act(() => {
    publishSessionState('rt-a', prompt(false))
    publishSessionState('rt-b', prompt(true))
  })

  expect(renders).toBe(before)
})

// [daveey] self-heal desktop-e6cce5965c: a done chat still watching a card drew the same plain green as "all done".
// It keeps the green, adds the eye glyph and names the pending check on hover.
it('a done chat that still watches a card draws the eye glyph and names the check', () => {
  const eta = new Date(2026, 10, 18, 9, 0).getTime() / 1000

  const session = row('watch-chat', {
    outcome: {
      at: 1,
      kind: 'done',
      source: 'derived',
      summary: 'Fix landed.',
      watches: [{ eta, id: 't_watch1', kind: 'card', label: 'verify the re-run limit', state: 'watching' }]
    }
  } as Partial<SessionInfo>)

  const { container } = render(<SessionRowStatusStripe outcomePainted session={session} statusStripe="done" />)

  expect(container.querySelector('[data-testid="stripe-watch"]')).not.toBeNull()
  expect(stripe(container)?.getAttribute('aria-label')).toBe('Fix landed.\nStill watching: t_watch1 (wakes Wed 09:00)')
})

it('a plain done chat draws no watch glyph', () => {
  const session = row('done-chat', {
    outcome: { at: 1, kind: 'done', source: 'derived', summary: 'Fix landed.' }
  } as Partial<SessionInfo>)

  const { container } = render(<SessionRowStatusStripe outcomePainted session={session} statusStripe="done" />)

  expect(container.querySelector('[data-testid="stripe-watch"]')).toBeNull()
  expect(stripe(container)?.getAttribute('aria-label')).toBe('Fix landed.')
})
