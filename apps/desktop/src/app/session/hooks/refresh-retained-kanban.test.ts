import { describe, expect, it } from 'vitest'

import { kanbanStripeCounts } from '@/store/session-dot-state'
import type { SessionInfo } from '@/types/hermes'

import { refreshRetainedKanban, withKanbanEvidence } from './refresh-retained-kanban'

const row = (extra = {}) => ({ id: 'old', profile: 'default', title: 'Keep me', ...extra }) as SessionInfo

describe('retained older sidebar task evidence', () => {
  it('fills an unannotated detail row without replacing its metadata', async () => {
    const [result] = await refreshRetainedKanban([row()], async () =>
      row({ title: 'stale', kanban_active: 16, kanban_observed_at: 123 })
    )
    expect(result).toMatchObject({ title: 'Keep me', kanban_active: 16, kanban_observed_at: 123 })
  })

  it('clears old counts when authoritative zero omits them', async () => {
    const [result] = await refreshRetainedKanban([row({ kanban_active: 16, kanban_unknown: true })], async () =>
      row({ kanban_observed_at: 124 })
    )
    expect(result.kanban_active).toBeUndefined()
    expect(result.kanban_unknown).toBeUndefined()
    expect(result.kanban_observed_at).toBe(124)
  })

  it('preserves last known counts but marks a failed read unknown', async () => {
    const [result] = await refreshRetainedKanban([row({ kanban_active: 16 })], async () => {
      throw new Error('offline')
    })
    expect(result).toMatchObject({ kanban_active: 16, kanban_unknown: true })
  })

  it.each([
    {},
    { kanban_observed_at: null },
    { kanban_observed_at: '123' },
    { kanban_observed_at: NaN },
    { kanban_observed_at: Infinity },
    { kanban_observed_at: -1 },
    { kanban_observed_at: 124, kanban_active: -1 },
    { kanban_observed_at: 124, kanban_active: '16' },
    { kanban_observed_at: 124, kanban_unknown: true }
  ])('preserves counts for absent or invalid evidence %j', async evidence => {
    const [result] = await refreshRetainedKanban([row({ kanban_active: 16, kanban_observed_at: 123 })], async () =>
      row(evidence)
    )
    expect(result).toMatchObject({ kanban_active: 16, kanban_observed_at: 123, kanban_unknown: true })
  })

  it('rejects another profile with the same session id', async () => {
    const [result] = await refreshRetainedKanban([row()], async () => row({ profile: 'other', kanban_active: 99 }))
    expect(result.kanban_unknown).toBe(true)
    expect(result.kanban_active).toBeUndefined()
  })

  // [daveey] needs_you is authoritative for the red stripe and omitted when empty: a fresh snapshot without it must
  // clear the old copy, or the row stays 'stuck' after its card unblocked or was archived.
  it('clears a stale needs_you when the fresh snapshot omits it', async () => {
    const ask = { kind: 'card', question: '', ref: 'default/t_a' }
    const old = row({ kanban_blocked: 1, kanban_observed_at: 1, needs_you: [ask] })
    const [result] = await refreshRetainedKanban([old], async () => row({ kanban_observed_at: 2 }))
    expect(result.needs_you).toBeUndefined()
    expect(result.kanban_blocked).toBeUndefined()
    expect(kanbanStripeCounts(result).blocked).toBe(0)
    expect(withKanbanEvidence(row(), row({ kanban_observed_at: 3, needs_you: [ask] })).needs_you).toEqual([ask])
  })
})
