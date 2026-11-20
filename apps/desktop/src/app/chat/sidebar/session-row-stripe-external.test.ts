import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import { unknownExternalCloud } from './session-row-stripe-external'

const gh = (over: Record<string, unknown> = {}) => ({
  adapter: 'github_run',
  health: 'ok',
  job: 'running',
  next_due_at: 1_300,
  observed_at: 1_000,
  reason: "job 'tests', 12 min",
  service: 'GitHub Actions',
  ...over
})

describe('unknownExternalCloud', () => {
  it('is hollow from stamped unknown counts', () => {
    const session = { kanban_external: 1, kanban_external_unknown: 1 } as SessionInfo

    expect(unknownExternalCloud(session, 'external', 1_000)).toBe(true)
  })

  it('is hollow from client-derived unknown even when stamps are absent', () => {
    const session = {
      kanban_external_cards: [{ external: gh(), task_id: 't_u' }]
    } as SessionInfo

    expect(unknownExternalCloud(session, 'external', 1_301)).toBe(true)
  })

  it('is solid when a running card is still current', () => {
    const session = {
      kanban_external: 1,
      kanban_external_cards: [{ external: gh(), task_id: 't_r' }]
    } as SessionInfo

    expect(unknownExternalCloud(session, 'external', 1_000)).toBe(false)
  })
})
