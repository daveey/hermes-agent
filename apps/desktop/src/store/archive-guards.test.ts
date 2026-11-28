import { afterEach, describe, expect, it, vi } from 'vitest'

import { createPluginContext } from '@/contrib/plugin'

import {
  collectArchiveLinks,
  kanbanArchiveLinks,
  registerArchiveGuard,
  resetArchiveGuardsForTests
} from './archive-guards'

const ctx = { ids: ['a'], sessionId: 'a' }

describe('archive guards', () => {
  afterEach(() => {
    vi.useRealTimers()
    resetArchiveGuardsForTests()
  })

  it('collects every guard in registration order and drops malformed lines', async () => {
    registerArchiveGuard('one', () => [{ label: 'Machines', value: '1 running' }])
    registerArchiveGuard('two', async () => [
      { label: 'Jobs', value: '2' },
      { label: '', value: 'x' },
      { bogus: true } as never
    ])
    registerArchiveGuard('three', () => null)

    expect(await collectArchiveLinks(ctx)).toEqual([
      { label: 'Machines', value: '1 running' },
      { label: 'Jobs', value: '2' }
    ])
  })

  it('skips a guard that throws or misses the deadline', async () => {
    registerArchiveGuard('boom', () => {
      throw new Error('nope')
    })
    registerArchiveGuard('slow', () => new Promise(() => {}))
    registerArchiveGuard('ok', () => [{ label: 'Machines', value: '1' }])

    expect(await collectArchiveLinks(ctx, 20)).toEqual([{ label: 'Machines', value: '1' }])
  })

  it('disposer removes only its own registration', async () => {
    const first = vi.fn(() => [{ label: 'A', value: '1' }])
    const dispose = registerArchiveGuard('same', first)
    registerArchiveGuard('same', () => [{ label: 'B', value: '2' }])
    dispose()

    expect(await collectArchiveLinks(ctx)).toEqual([{ label: 'B', value: '2' }])
    expect(first).not.toHaveBeenCalled()
  })

  it('plugin ctx.registerArchiveGuard is removed when the plugin unloads', async () => {
    const disposers: Array<() => void> = []
    const plugin = createPluginContext('session-tasks', dispose => disposers.push(dispose))
    plugin.registerArchiveGuard(() => [{ label: 'Machines', value: '1' }])

    expect(await collectArchiveLinks(ctx)).toHaveLength(1)
    disposers.forEach(dispose => dispose())
    expect(await collectArchiveLinks(ctx)).toEqual([])
  })

  it('keeps unknown task status visible even with zero known counts', () => {
    const copy = { label: 'Kanban tasks', unknown: 'Status unavailable; work may be unfinished', value: () => 'counts' }

    expect(kanbanArchiveLinks({ kanban_unknown: true, kanban_active: 0 } as never, copy)).toEqual([
      { label: copy.label, value: copy.unknown }
    ])
    expect(kanbanArchiveLinks({ kanban_unknown: true, kanban_active: 2 } as never, copy)).toEqual([
      { label: copy.label, value: copy.unknown }
    ])
  })

  it.each([
    undefined,
    {},
    { kanban_observed_at: 1 },
    { kanban_observed_at: Infinity },
    { kanban_observed_at: Date.now() / 1000 + 60 }
  ])('warns when zero-work evidence is absent or stale: %j', session => {
    const copy = { label: 'Kanban tasks', unknown: 'Status unavailable', value: () => 'counts' }

    expect(kanbanArchiveLinks(session as never, copy)).toEqual([{ label: copy.label, value: copy.unknown }])
  })

  it('kanbanArchiveLinks reads the row counts', () => {
    const copy = {
      label: 'Kanban tasks',
      unknown: 'Status unavailable',
      value: (open: number, blocked: number) => `${open}/${blocked}`
    }

    expect(kanbanArchiveLinks({ kanban_active: 0, kanban_observed_at: Date.now() / 1000 } as never, copy)).toEqual([])
    expect(kanbanArchiveLinks({ kanban_active: 2, kanban_blocked: 1 } as never, copy)).toEqual([
      { label: 'Kanban tasks', value: '2/1' }
    ])
  })
})
