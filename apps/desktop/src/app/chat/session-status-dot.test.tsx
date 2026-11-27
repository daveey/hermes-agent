import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { $backgroundStatusBySession } from '@/store/composer-status'
import { $kanbanObservationTick } from '@/store/kanban-observation'
import { $sessions } from '@/store/session'
import { canonicalOutcome } from '@/test/canonical-outcome'
import type { SessionInfo } from '@/types/hermes'
import type { SessionOutcome } from '@/types/session-outcome'

import { sessionDotVariant, SessionStatusDot } from './session-status-dot'

afterEach(() => {
  cleanup()
  $backgroundStatusBySession.set({})
  $sessions.set([])
})

const mount = (outcome?: SessionOutcome) => render(
  <SessionStatusDot runningCardCount={2} session={{ id: 'chat', outcome, kanban_active: 3, kanban_blocked: 1 } as SessionInfo} storedSessionId="chat" />
)

describe('SessionStatusDot published status', () => {
  it('red overdue triage is agent attention, not a human decision', () => {
    const outcome = canonicalOutcome('stuck', { kind: 'waiting', presentation: { stripe: 'stuck', reason_codes: ['triage_overdue'] } })
    const { container } = mount(outcome)
    const dot = container.querySelector('[data-session-status-dot]')!
    expect(dot.getAttribute('data-session-status-dot')).toBe('stuck')
    expect(dot.getAttribute('aria-label')).toBe("Overdue triage · waiting on this chat's agent")
    expect(dot.className).toContain('--color-red-500')
  })
  it.each(['done', 'waiting', 'attention', 'blocked', 'stuck', 'none'] as const)(
    'paints %s independently of local activity, unread and card counts', stripe => {
      const { container } = mount(canonicalOutcome(stripe, {}, { kind: 'done' }))
      const dot = container.querySelector('[data-session-status-dot]')!
      expect(dot.getAttribute('data-session-status-dot')).toBe(stripe)
      expect(dot.getAttribute('role')).toBe('status')
      expect(dot.getAttribute('aria-label')).toBeTruthy()

      if (stripe === 'done') {expect(dot.className).toContain('bg-(--ui-success)')}

      if (stripe === 'none') {expect(dot.className).toContain('border-(--ui-text-quaternary)')}

      if (stripe === 'attention') {expect(dot.className).toContain('--color-amber-500')}
    }
  )

  it.each([
    ['running', 'waiting', 'Running'],
    ['waiting', 'running', 'Waiting']
  ] as const)('labels published %s without consulting legacy %s', (kind, legacyKind, label) => {
    const { container } = mount(canonicalOutcome('waiting', { kind }, { kind: legacyKind }))
    const dot = container.querySelector('[data-session-status-dot]')!
    expect(dot.getAttribute('data-session-status-dot')).toBe('waiting')
    expect(dot.getAttribute('aria-label')).toBe(label)
    expect(dot.getAttribute('title')).toBe(label)
    expect(dot.className).not.toContain('bg-(--ui-success)')
    expect(dot.className).toContain(kind === 'running' ? 'bg-(--ui-accent)' : 'bg-(--session-status-color)')

    if (kind === 'waiting') {expect(dot.className).not.toContain('bg-(--ui-accent)')}
  })

  it('announces a canonical never-started session as draft, not Running', () => {
    // Shape published by reduce_status for an empty transcript with complete source reads.
    const status = {
      ...canonicalOutcome('none').status,
      kind: 'unresolved',
      reason: 'empty',
      presentation: { stripe: 'none', reason_codes: ['never_started'] }
    }

    const { container } = mount({ kind: 'unresolved', status })
    const dot = container.querySelector('[data-session-status-dot]')!

    expect(dot.getAttribute('data-session-status-dot')).toBe('none')
    expect(dot.getAttribute('aria-label')).toBe('Draft — nothing sent yet')
    expect(dot.getAttribute('title')).toBe('Draft — nothing sent yet')
    expect(dot.className).toContain('border-(--ui-text-quaternary)')
  })

  it.each([undefined, { kind: 'done' }, { kind: 'done', status: { status_version: 2 } }] as const)(
    'missing and unsupported protocols remain unknown: %j', value => {
      const { container } = mount(value as SessionOutcome | undefined)
      const dot = container.querySelector('[data-session-status-dot]')!
      expect(dot.getAttribute('data-session-status-dot')).toBe('attention')
      expect(dot.getAttribute('aria-label')).toBe('Open work, current status unknown')
      expect(dot.className).not.toContain('bg-(--ui-success)')
    }
  )

  it('does not let roster or clock changes reclassify a published status', () => {
    const { container } = mount(canonicalOutcome('done'))
    const before = container.innerHTML
    act(() => {
      $backgroundStatusBySession.set({ chat: [{ id: 'job', state: 'running', title: 'job', type: 'background' }] })
      $kanbanObservationTick.set($kanbanObservationTick.get() + 1)
    })
    expect(container.innerHTML).toBe(before)
  })

  it('repaints on a new backend status rather than a client clock', () => {
    const view = mount(canonicalOutcome('done', { revision: 3 }))
    view.rerender(<SessionStatusDot session={{ id: 'chat', outcome: canonicalOutcome('stuck', { revision: 4 }) } as SessionInfo} storedSessionId="chat" />)
    expect(view.container.querySelector('[data-session-status-dot]')?.getAttribute('data-session-status-dot')).toBe('stuck')
  })

  it('activity variants cannot select a different published color', () => {
    const outcome = canonicalOutcome('done')
    const idle = sessionDotVariant('idle', outcome)

    for (const state of ['working', 'stalled', 'needs-input', 'unread', 'background', 'draft'] as const) {
      const variant = sessionDotVariant(state, outcome, 'external_down')
      expect(variant.className).toBe(idle.className)
      expect(variant.role).toBe(idle.role)
    }
  })
})
