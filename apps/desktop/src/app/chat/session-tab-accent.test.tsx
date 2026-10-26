import { act, cleanup, render } from '@testing-library/react'
import type { WritableAtom } from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PaneTab } from '@/components/ui/pane-tab'
import { $selectedStoredSessionId, $sessions } from '@/store/session'
import { $blockedSessionIds } from '@/store/session-blocked'
import { $sessionDotStateById, type SessionDotState } from '@/store/session-dot-state'
import type * as DotStateModule from '@/store/session-dot-state'
import type { SessionInfo } from '@/types/hermes'

import { SessionTabAccent } from './session-tab-accent'
import { SessionTabMenu, WorkspaceTabMenu } from './session-tile'

vi.mock('@/store/session-dot-state', async importOriginal => ({
  ...(await importOriginal<typeof DotStateModule>()),
  $sessionDotStateById: (await import('nanostores')).atom<Record<string, SessionDotState>>({})
}))

const states = $sessionDotStateById as WritableAtom<Record<string, SessionDotState>>
const row = { id: 'session', message_count: 2, kanban_observed_at: Date.now() / 1000 } as SessionInfo

function tab(session: SessionInfo | null = row) {
  return (
    <SessionTabAccent session={session} storedSessionId={session?.id ?? null}>
      <PaneTab active>Session</PaneTab>
    </SessionTabAccent>
  )
}

afterEach(() => {
  cleanup()
  $blockedSessionIds.set([])
  $selectedStoredSessionId.set(null)
  $sessions.set([])
  states.set({})
})

describe('session tab underline', () => {
  it('scopes the accent on the real workspace and tile tab wrappers', () => {
    const { container, rerender } = render(
      <WorkspaceTabMenu>
        <PaneTab active>New session</PaneTab>
      </WorkspaceTabMenu>
    )

    expect(container.querySelector('[data-session-tab-accent="neutral"]')).not.toBeNull()

    act(() => {
      $sessions.set([row])
      $selectedStoredSessionId.set(row.id)
    })
    expect(container.querySelector('[data-session-tab-accent="done"]')).not.toBeNull()
    rerender(
      <SessionTabMenu storedSessionId={row.id} tabPaneId={`session-tile:${row.id}`}>
        <PaneTab active>Tile</PaneTab>
      </SessionTabMenu>
    )
    expect(container.querySelector('[data-session-tab-accent="done"]')).not.toBeNull()
    act(() => states.set({ session: 'needs-input' }))
    expect(container.querySelector('[data-session-tab-accent="stuck"]')).not.toBeNull()
  })

  it.each([
    ['idle', 'done', 'emerald-500'],
    ['unread', 'done', 'emerald-500'],
    ['needs-input', 'stuck', 'red-500'],
    ['stalled', 'stuck', 'red-500'],
    ['working', 'neutral', 'zinc-400'],
    ['background', 'neutral', 'zinc-400'],
    ['draft', 'neutral', 'zinc-400']
  ] as const)('uses the sidebar palette for %s', (state, stripe, color) => {
    states.set({ session: state })
    const { container } = render(tab())
    const accent = container.querySelector('[data-session-tab-accent]')!

    expect(accent.getAttribute('data-session-tab-accent')).toBe(stripe)
    expect(accent.className).toContain(`--color-${color}`)
    expect((accent as HTMLElement).style.getPropertyValue('--pane-tab-active-accent')).toBe(
      'var(--session-status-color)'
    )
  })

  it('reacts to turn, manual blocking, and row metadata changes', () => {
    const { container, rerender } = render(tab())
    const stripe = () => container.querySelector('[data-session-tab-accent]')!.getAttribute('data-session-tab-accent')

    expect(stripe()).toBe('done')
    act(() => states.set({ session: 'needs-input' }))
    expect(stripe()).toBe('stuck')
    act(() => $blockedSessionIds.set(['session']))
    expect(stripe()).toBe('blocked')
    act(() => {
      $blockedSessionIds.set([])
      states.set({})
    })
    rerender(tab({ ...row, cron_active: true }))
    expect(stripe()).toBe('periodic')
    rerender(tab({ ...row, kanban_blocked: 1 }))
    expect(stripe()).toBe('stuck')
    rerender(tab({ ...row, archived: true }))
    expect(stripe()).toBe('neutral')
    rerender(tab(null))
    expect(stripe()).toBe('neutral')
    expect(container.firstElementChild!.className).toContain('dark:[--session-status-color:var(--color-zinc-500)]')
  })

  it.each([{ kanban_observed_at: undefined }, { kanban_unknown: true }, { kanban_active: 1 }, { kanban_observed_at: 1 }])('does not mark unverified tree-only rows done: %j', evidence => {
    const { container } = render(tab({ ...row, ...evidence }))

    expect(container.querySelector('[data-session-tab-accent="done"]')).toBeNull()
  })

  it('does not finish an unread stored tab whose row is missing', () => {
    states.set({ session: 'unread' })
    const { container } = render(<SessionTabAccent storedSessionId="session"><span>Missing</span></SessionTabAccent>)
    expect(container.querySelector('[data-session-tab-accent="done"]')).toBeNull()
  })

  it('leaves non-session tabs on their existing theme accent', () => {
    const { container } = render(<PaneTab active>Files</PaneTab>)

    expect(container.querySelector('[data-session-tab-accent]')).toBeNull()
    expect(container.firstElementChild!.className).toContain('var(--pane-tab-active-accent,var(--theme-primary))')
    expect((container.firstElementChild as HTMLElement).style.getPropertyValue('--pane-tab-active-accent')).toBe('')
  })
})
