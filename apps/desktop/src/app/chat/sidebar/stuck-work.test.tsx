import { expect, it, vi } from 'vitest'

import { sessionHasOpenCards } from '@/store/session-filter'
import { sessionHiddenAsAutomated } from '@/store/sidebar-automated'
import { makeSessionInfo } from '@/test/session-info'

import { sessionCardColours } from './session-row-card-squares'

const cardEvidence = (id: string, now: number, state = 'running') => ({
  version: 1,
  identity: { gateway: 'hub', profile: 'default', board: 'default', kind: 'card', work_id: id, run_id: 7 },
  owner: { gateway: 'hub', profile: 'default' },
  state,
  source: 'kanban.card',
  source_revision: 'beat:7',
  evidence_refs: ['beat:7'],
  observed_at: now,
  valid_until: now + 180,
  last_activity: now - 7200
})

it('keeps a quiet-only automated row using the captured render clock, not the ambient clock', () => {
  const now = 1_000_000
  const row = makeSessionInfo({ automated: true, kanban_active: 1, execution_observations: [cardEvidence('q', now)] })
  const ctx = { pinnedIds: [], userBlocked: false, dotState: 'background' as const, nowSeconds: now }
  expect(sessionHiddenAsAutomated(row, false, ctx)).toBe(false)
  expect(sessionHiddenAsAutomated({ ...row, execution_observations: [] }, false, ctx)).toBe(true)
  expect(sessionHiddenAsAutomated(row, false, { ...ctx, nowSeconds: now + 181 })).toBe(true)
})

it('caps quiet marks by live cards after queued, parked and held counts', () => {
  vi.useFakeTimers()
  const now = Date.now() / 1000

  const row = makeSessionInfo({
    kanban_active: 4,
    kanban_parked: 1,
    kanban_held: 1,
    execution_observations: [cardEvidence('q1', now), cardEvidence('q2', now), cardEvidence('ready', now, 'queued')]
  })

  expect(sessionCardColours(row, 0)).toEqual([
    { colour: 'pink', count: 1 },
    { colour: 'orange', count: 1 },
    { colour: 'green', count: 1 },
    { colour: 'grey', count: 1 }
  ])
  expect(sessionCardColours({ ...row, kanban_active: 2 }, 0)).toEqual([
    { colour: 'pink', count: 1 },
    { colour: 'grey', count: 1 }
  ])
  vi.useRealTimers()
})

it('keeps stuck-only work and distinguishes it from ready and running', () => {
  const row = makeSessionInfo({ kanban_stalled: 4 })
  expect(sessionHasOpenCards(row)).toBe(true)
  expect(sessionCardColours(row, 0)).toEqual([{ colour: 'orange', count: 4 }])
})

it('does not hide an ordinary automated stuck session just because other work runs', () => {
  const row = makeSessionInfo({
    id: 'ordinary',
    automated: true,
    kanban_active: 8,
    kanban_parked: 4,
    kanban_stalled: 4,
    kanban_observed_at: Date.now() / 1000
  })

  expect(sessionHiddenAsAutomated(row, false, { pinnedIds: [], userBlocked: false, dotState: 'background' })).toBe(
    false
  )
  expect(sessionCardColours(row, 0)).toEqual([
    { colour: 'orange', count: 4 },
    { colour: 'blue', count: 4 }
  ])
})

it('separates current quiet running evidence from live progress without inventing a human ask', () => {
  const now = Date.now() / 1000

  const evidence = (id: string, activity: number) => ({
    version: 1,
    identity: { gateway: 'hub', profile: 'default', board: 'default', kind: 'card', work_id: id, run_id: 7 },
    owner: { gateway: 'hub', profile: 'default' },
    state: 'running',
    source: 'kanban.card',
    source_revision: 'beat:7',
    evidence_refs: ['beat:7'],
    observed_at: now,
    valid_until: now + 180,
    last_activity: activity
  })

  const quiet = evidence('quiet', now - 7200)
  const row = makeSessionInfo({ kanban_active: 2, execution_observations: [quiet, quiet, evidence('live', now)] })
  expect(sessionCardColours(row, 0)).toEqual([
    { colour: 'orange', count: 1 },
    { colour: 'blue', count: 1 }
  ])
  expect(
    sessionCardColours({ ...row, execution_observations: [{ ...quiet, valid_until: now - 1 }] }, 0)
  ).not.toContainEqual({ colour: 'orange', count: 1 })
  expect(
    sessionCardColours({ ...row, execution_observations: [{ ...quiet, last_activity: undefined }] }, 0)
  ).not.toContainEqual({ colour: 'orange', count: 1 })
  expect(
    sessionCardColours({ ...row, execution_observations: [quiet, { ...quiet, state: 'queued' }] }, 0)
  ).not.toContainEqual({ colour: 'orange', count: 1 })
  vi.useRealTimers()
})
