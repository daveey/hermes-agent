import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import { refreshRetainedKanban } from './refresh-retained-kanban'

const row = (extra = {}) => ({ id: 'old', profile: 'default', title: 'Keep me', ...extra }) as SessionInfo

describe('retained older sidebar task evidence', () => {
  it('fills an unannotated detail row without replacing its metadata', async () => {
    const [result] = await refreshRetainedKanban([row()], async () => row({ title: 'stale', kanban_active: 16, kanban_observed_at: 123 }))
    expect(result).toMatchObject({ title: 'Keep me', kanban_active: 16, kanban_observed_at: 123 })
  })

  it('clears old counts when authoritative zero omits them', async () => {
    const [result] = await refreshRetainedKanban([row({ kanban_active: 16, kanban_unknown: true })], async () => row({ kanban_observed_at: 124 }))
    expect(result.kanban_active).toBeUndefined()
    expect(result.kanban_unknown).toBeUndefined()
    expect(result.kanban_observed_at).toBe(124)
  })

  it('preserves last known counts but marks a failed read unknown', async () => {
    const [result] = await refreshRetainedKanban([row({ kanban_active: 16 })], async () => { throw new Error('offline') })
    expect(result).toMatchObject({ kanban_active: 16, kanban_unknown: true })
  })

  it.each([{}, { kanban_observed_at: null }, { kanban_observed_at: '123' },
    { kanban_observed_at: NaN }, { kanban_observed_at: Infinity }, { kanban_observed_at: -1 },
    { kanban_observed_at: 124, kanban_active: -1 }, { kanban_observed_at: 124, kanban_active: '16' },
    { kanban_observed_at: 124, kanban_unknown: true }])('preserves counts for absent or invalid evidence %j', async evidence => {
    const [result] = await refreshRetainedKanban([row({ kanban_active: 16, kanban_observed_at: 123 })], async () => row(evidence))
    expect(result).toMatchObject({ kanban_active: 16, kanban_observed_at: 123, kanban_unknown: true })
  })

  it('rejects another profile with the same session id', async () => {
    const [result] = await refreshRetainedKanban([row()], async () => row({ profile: 'other', kanban_active: 99 }))
    expect(result.kanban_unknown).toBe(true)
    expect(result.kanban_active).toBeUndefined()
  })
})
