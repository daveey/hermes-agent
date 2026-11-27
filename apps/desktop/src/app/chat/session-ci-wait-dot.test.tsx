import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

import { cleanup, render } from '@testing-library/react'
import { afterEach, beforeAll, expect, it } from 'vitest'

import { canonicalOutcome } from '@/test/canonical-outcome'
import type { SessionInfo } from '@/types/hermes'

import { SessionStatusDot } from './session-status-dot'

// Run the production card adapter, canonical reducer and list-row serializer, not hand-authored outcomes.
let rows: Record<string, SessionInfo>

beforeAll(() => {
  rows = JSON.parse(
    execFileSync('scripts/hermes-python', ['tests/fixtures/ci_wait_dot_projection.py'], {
      cwd: resolve(process.cwd(), '../..'),
      encoding: 'utf8',
      env: { ...process.env, PYTHONPATH: resolve(process.cwd(), '../..') },
      timeout: 90_000
    })
  ) as Record<string, SessionInfo>
}, 120_000)

afterEach(cleanup)

it('a confirmed queued CI producer paints light blue, without claiming the owner is running', () => {
  const row = rows.queued_ci
  expect(row.outcome?.status?.kind).toBe('waiting')
  const { container } = render(<SessionStatusDot session={row} storedSessionId={row.id} />)
  const dot = container.querySelector('[data-session-status-dot]')!
  expect(dot.getAttribute('data-session-status-dot')).toBe('external')
  expect((dot as HTMLElement).style.backgroundColor).toBe('rgb(56, 189, 248)')
  expect(dot.getAttribute('aria-label')).toBe('Waiting')
})

it.each(['unknown', 'annotation_unknown'])('%s is explicitly unknown, not blue or confident gray idle', kind => {
  const row = kind === 'unknown' ? rows.unknown : { ...rows.queued_ci, kanban_unknown: true }
  const { container } = render(<SessionStatusDot session={row} storedSessionId={row.id} />)
  const dot = container.querySelector('[data-session-status-dot]')!
  expect(dot.getAttribute('aria-label')).toBe('Open work, current status unknown')
  expect(dot.getAttribute('data-session-status-dot')).toBe('attention')
  expect(dot.className).not.toContain('#38bdf8')
  expect(dot.className).not.toContain('border-(--ui-text-quaternary)')
})

it.each([
  ['running', 'waiting'],
  ['decision', 'stuck'],
  ['muted', 'blocked']
] as const)('unknown annotations do not erase independent %s status', (kind, stripe) => {
  const row = { ...rows.empty, kanban_unknown: true, outcome: canonicalOutcome(stripe, { kind }) }
  const { container } = render(<SessionStatusDot session={row} storedSessionId={row.id} />)
  expect(container.querySelector(`[data-session-status-dot="${stripe}"]`)).not.toBeNull()
})

it('authoritative no-work stays gray', () => {
  const row = rows.empty
  const { container } = render(<SessionStatusDot session={row} storedSessionId={row.id} />)
  const dot = container.querySelector('[data-session-status-dot]')!
  expect(dot.getAttribute('data-session-status-dot')).toBe('none')
  expect(dot.className).toContain('border-(--ui-text-quaternary)')
})
