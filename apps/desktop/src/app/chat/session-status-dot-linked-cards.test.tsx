import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SessionDotState } from '@/store/session-dot-state'

import { SessionStatusDot } from './session-status-dot'

const state = vi.hoisted(() => ({ value: 'idle' as SessionDotState }))
vi.mock('@/lib/use-session-dot-state', () => ({
  useSessionDotState: () => state.value,
  useBackgroundDotLabel: () => 'running'
}))
vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        row: {
          sessionRunning: 'Running',
          needsInput: 'Needs input',
          waitingForAnswer: 'Waiting',
          finishedUnread: 'Finished',
          backgroundRunning: 'Background',
          draftSession: 'Draft'
        }
      }
    }
  })
}))
afterEach(cleanup)

function dot(value: SessionDotState, runningCardCount?: number) {
  state.value = value
  const view = render(<SessionStatusDot runningCardCount={runningCardCount} storedSessionId="s1" />)
  const element = view.container.querySelector('.rounded-full')!

  const result = {
    className: element.className,
    aria: element.getAttribute('aria-label'),
    background: element.getAttribute('data-background-label'),
    role: element.getAttribute('role')
  }

  view.unmount()

  return result
}

describe('linked-card activity cannot classify missing canonical status', () => {
  it.each(['idle', 'draft', 'background', 'stalled'] as const)(
    'keeps %s unknown just like a working claim without published status',
    value => {
      expect(dot(value, 2)).toEqual(dot('working', 0))
    }
  )
  it.each(['needs-input', 'unread'] as const)('preserves %s precedence', value => {
    expect(dot(value, 2)).toEqual(dot(value, 0))
  })
  it('keeps callers without card input unchanged', () => {
    expect(dot('idle')).toEqual(dot('idle', 0))
    expect(dot('idle').className).toContain('amber-500')
    expect(dot('working').className).toContain('amber-500')
  })
})
