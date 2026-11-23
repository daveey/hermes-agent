import { compile } from '@tailwindcss/node'
import { act, cleanup, render, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $queuedPromptsBySession } from '@/store/composer-queue'
import { $kanbanObservationTick } from '@/store/kanban-observation'
import { reconcilePendingSubmissions } from '@/store/pending-submissions'
import { $sessions, setSessions } from '@/store/session'
import { resetStripeTransitionLog, setStripeReasonReporter } from '@/store/session-dot-state'
import { clearAllSessionStates, publishSessionState } from '@/store/session-states'
import { canonicalOutcome } from '@/test/canonical-outcome'
import type { SessionInfo } from '@/types/hermes'

import { SessionRowStatusStripe, useSessionRowStripe } from './session-row-stripe'

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 4, source: 'desktop', started_at: 0, title: id, ...extra }) as SessionInfo

it.each(['ci', 'deploy'])('a replica %s park paints the canonical light-blue hollow cloud', () => {
  const session = row('replica-wait', {
    kanban_active: 1, kanban_parked: 1, kanban_external: 1, kanban_external_unknown: 1,
    outcome: canonicalOutcome('external')
  })

  setSessions([session])
  const { result } = renderHook(() => useSessionRowStripe(session, 'background', true))
  expect(result.current.statusStripe).toBe('external')

  const { container } = render(
    <SessionRowStatusStripe outcomePainted={result.current.outcomePainted} session={session}
      statusStripe={result.current.statusStripe} />
  )

  expect(stripe(container)?.getAttribute('data-status-stripe')).toBe('external')
  expect(stripe(container)?.getAttribute('data-external-unknown')).toBe('true')
  expect(stripe(container)?.className).toContain('--color-sky-400')
})

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
      status: canonicalOutcome('stuck').status,
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

it("the chat's own live turn cannot veto the published decision stripe", () => {
  const session = row('working-chat', {
    outcome: {
      at: 1,
      items: [{ choices: [], id: 'req-2', question: 'Q?', state: 'open' }],
      kind: 'decision',
      status: canonicalOutcome('stuck').status,
      source: 'derived'
    }
  } as Partial<SessionInfo>)

  setSessions([session])

  const { result } = renderHook(() => useSessionRowStripe(session, 'working', true))

  expect(result.current.outcomePainted).toBe(true)
  expect(result.current.statusStripe).toBe('stuck')
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

  const session = row('red-chat', { outcome: canonicalOutcome('stuck', {}, { kind: 'decision' }) })

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
  vi.useFakeTimers({ now: new Date(2026, 10, 13, 12, 0), toFake: ['Date'] })
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

  try {
    const { container } = render(<SessionRowStatusStripe outcomePainted session={session} statusStripe="done" />)

    expect(container.querySelector('[data-testid="stripe-watch"]')).not.toBeNull()
    // [daveey] PR #2049 review: the card's title leads, with its id kept for lookup.
    expect(stripe(container)?.getAttribute('aria-label')).toBe(
      'Fix landed.\nStill watching: verify the re-run limit (t_watch1, wakes Wed 09:00)'
    )
  } finally {
    vi.useRealTimers()
  }
})

it('a plain done chat draws no watch glyph', () => {
  const session = row('done-chat', {
    outcome: { at: 1, kind: 'done', source: 'derived', summary: 'Fix landed.' }
  } as Partial<SessionInfo>)

  const { container } = render(<SessionRowStatusStripe outcomePainted session={session} statusStripe="done" />)

  expect(container.querySelector('[data-testid="stripe-watch"]')).toBeNull()
  expect(stripe(container)?.getAttribute('aria-label')).toBe('Fix landed.')
})

const ghCard = (over: Record<string, unknown> = {}) => ({
  external: {
    adapter: 'github_run',
    health: 'ok',
    job: 'running',
    next_due_at: 1300,
    observed_at: 1000,
    reason: "job 'tests', 12 min",
    service: 'GitHub Actions',
    url: 'https://github.com/org/repo/actions/runs/1',
    ...over
  },
  task_id: 't_ext1',
  title: 'Wait on CI'
})

it('external stripe paints a cloud glyph and names the service', () => {
  vi.useFakeTimers()
  vi.setSystemTime(1_000_000)

  try {
    const session = row('ext-chat', {
      kanban_external: 1,
      kanban_external_cards: [ghCard()],
      kanban_parked: 1
    })

    const { container } = render(
      <SessionRowStatusStripe outcomePainted={false} session={session} statusStripe="external" />
    )

    expect(container.querySelector('.codicon-cloud')).not.toBeNull()
    expect(stripe(container)?.getAttribute('data-status-stripe')).toBe('external')
    expect(stripe(container)?.getAttribute('aria-label')).toContain('Running on GitHub Actions')
  } finally {
    vi.useRealTimers()
  }
})

it('a mounted external stripe expires on the client clock with an unchanged session', () => {
  vi.useFakeTimers()
  vi.setSystemTime(1_000_000)

  try {
    const session = row('ext-mounted', { kanban_external_cards: [ghCard()], kanban_parked: 1 })

    const { container } = render(
      <SessionRowStatusStripe outcomePainted={false} session={session} statusStripe="external" />
    )

    expect(container.querySelector('[data-external-cloud="solid"]')).not.toBeNull()
    expect(stripe(container)?.getAttribute('aria-label')).toContain('Running on GitHub Actions')
    act(() => {
      vi.setSystemTime(1_301_000)
      $kanbanObservationTick.set($kanbanObservationTick.get() + 1)
    })
    expect(container.querySelector('[data-external-cloud="hollow"]')).not.toBeNull()
    expect(stripe(container)?.getAttribute('aria-label')).toContain('Status unknown on GitHub Actions')
  } finally {
    cleanup()
    vi.useRealTimers()
  }
})

it('unknown external uses the hollow cloud', () => {
  const session = row('ext-unk', {
    kanban_external: 1,
    kanban_external_unknown: 1,
    kanban_parked: 1
  })

  const { container } = render(
    <SessionRowStatusStripe outcomePainted={false} session={session} statusStripe="external" />
  )

  expect(container.querySelector('[data-external-unknown="true"]')).not.toBeNull()
  expect(container.querySelector('[data-external-cloud="hollow"]')).not.toBeNull()
})

it('unknown external is hollow from client-derived state without stamps', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(1_301_000)

  try {
    const session = row('ext-derived-unk', {
      kanban_external_cards: [ghCard()],
      kanban_parked: 1
    })

    const solidSession = row('ext-solid', {
      kanban_external_cards: [ghCard({ next_due_at: 2000, observed_at: 1301 })],
      kanban_parked: 1
    })

    const { container } = render(
      <>
        <SessionRowStatusStripe outcomePainted={false} session={session} statusStripe="external" />
        <SessionRowStatusStripe outcomePainted={false} session={solidSession} statusStripe="external" />
      </>
    )

    // jsdom does not resolve custom-property colours; supply the stripe's resolved sky colour.
    container.querySelectorAll<HTMLElement>('[data-slot="session-status-stripe"]').forEach(el => {
      el.style.color = 'rgb(56, 189, 248)'
    })
    const cloud = container.querySelector<HTMLElement>('[data-external-cloud="hollow"]')!

    const { build } = await compile('@tailwind utilities;', {
      base: new URL('.', import.meta.url).pathname,
      onDependency() {}
    })

    const stylesheet = window.document.createElement('style')
    stylesheet.textContent = build([...container.querySelectorAll('[class]')].flatMap(el => [...el.classList]))
    window.document.head.append(stylesheet)

    try {
      const solid = container.querySelector<HTMLElement>('[data-external-cloud="solid"]')!
      const solidStyle = getComputedStyle(solid)
      const style = getComputedStyle(cloud)
      // Require an opaque colour, not merely a hollow marker or a stroke utility.
      expect(solidStyle.color).toBe('rgb(56, 189, 248)')
      expect(style.getPropertyValue('-webkit-text-stroke-color')).toBe(solidStyle.color)
      expect(style.getPropertyValue('-webkit-text-fill-color')).toBe('rgba(0, 0, 0, 0)')
      expect(solidStyle.getPropertyValue('-webkit-text-fill-color')).toBe(solidStyle.color)
    } finally {
      stylesheet.remove()
    }
  } finally {
    vi.useRealTimers()
  }
})

it('external_down stripe is red with a cloud and down hover', () => {
  vi.useFakeTimers()
  vi.setSystemTime(1_000_000)

  try {
    const session = row('ext-down', {
      kanban_external_cards: [ghCard({ health: 'unreachable', job: 'unknown', reason: 'host unreachable' })],
      kanban_external_down: 1
    })

    const { container } = render(
      <SessionRowStatusStripe outcomePainted={false} session={session} statusStripe="external_down" />
    )

    expect(stripe(container)?.getAttribute('data-status-stripe')).toBe('external_down')
    expect(container.querySelector('.codicon-cloud')).not.toBeNull()
    expect(stripe(container)?.getAttribute('aria-label')).toMatch(/unreachable|GitHub Actions/)
  } finally {
    vi.useRealTimers()
  }
})

it('sidebar stripe lists each external card then +N more', () => {
  vi.useFakeTimers()
  vi.setSystemTime(1_000_000)

  try {
    const cards = Array.from({ length: 7 }, (_, i) => ({
      ...ghCard(),
      task_id: `t_${i}`,
      title: `Card ${i}`
    }))

    const session = row('ext-many', { kanban_external: 7, kanban_external_cards: cards })

    const { container } = render(
      <SessionRowStatusStripe outcomePainted={false} session={session} statusStripe="external" />
    )

    const label = stripe(container)?.getAttribute('aria-label') ?? ''

    expect(label).toContain('Card 0')
    expect(label).toContain('Card 4')
    expect(label).not.toContain('Card 5')
    expect(label).toContain('+2 more')
  } finally {
    vi.useRealTimers()
  }
})

it('missing external fields keep today: no Running-on claim on a waiting stripe', () => {
  const { container } = render(
    <SessionRowStatusStripe outcomePainted={false} session={row('plain')} statusStripe="waiting" />
  )

  expect(stripe(container)?.getAttribute('aria-label')).toBeNull()
  expect(container.querySelector('.codicon-cloud')).toBeNull()
})

// [daveey] A timer (clock wait) joins CI and deploy as the canonical light-blue stripe, drawn as a plain bar: no cloud.
it('a clock wait paints the canonical sky-400 stripe without a cloud', () => {
  const session = row('clock-wait', {
    kanban_active: 1, kanban_parked: 1, kanban_external: 1,
    kanban_external_cards: [{
      eta: { estimate_source: 'wakes 14:30', expected_s: 14_400, wait_kind: 'clock', waited_since: 1 },
      external: { adapter: 'clock', health: 'ok', job: 'running', service: 'timer' },
      task_id: 't_clock'
    }],
    outcome: canonicalOutcome('external')
  })

  setSessions([session])
  const { result } = renderHook(() => useSessionRowStripe(session, 'background', true))
  expect(result.current.statusStripe).toBe('external')

  const { container } = render(
    <SessionRowStatusStripe outcomePainted={result.current.outcomePainted} session={session}
      statusStripe={result.current.statusStripe} />
  )

  expect(stripe(container)?.getAttribute('data-status-stripe')).toBe('external')
  expect(stripe(container)?.className).toContain('--color-sky-400')
  expect(container.querySelector('[data-external-cloud]')).toBeNull()
  expect(stripe(container)?.className).toContain('w-[3px]') // the plain bar, not the glyph
})

it('a CI wait still draws its cloud', () => {
  const session = row('ci-wait', {
    kanban_active: 1, kanban_parked: 1, kanban_external: 1,
    kanban_external_cards: [{ external: { adapter: 'github_run', health: 'ok', job: 'running', service: 'GitHub Actions' }, task_id: 't_ci' }],
    outcome: canonicalOutcome('external')
  })

  const { container } = render(
    <SessionRowStatusStripe outcomePainted={true} session={session} statusStripe="external" />
  )

  expect(container.querySelector('[data-external-cloud]')).not.toBeNull()
})
