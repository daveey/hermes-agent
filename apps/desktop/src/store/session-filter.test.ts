import { afterEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'
import type { Contribution } from '@/contrib/types'
import { resetSidebarView } from '@/store/layout'
import { makeSessionInfo } from '@/test/session-info'

import {
  applySessionFilters,
  clearSessionFilterContributions,
  SESSION_FILTER_AREA,
  sessionHasOpenCards
} from './session-filter'

const session = makeSessionInfo({ id: 's1', title: 'one' })

const c = (id: string, data: unknown): Contribution => ({ area: SESSION_FILTER_AREA, data, id })

afterEach(() => vi.restoreAllMocks())

describe('applySessionFilters', () => {
  it('matches everything when there are no contributions', () => {
    expect(applySessionFilters(session, [])).toBe(true)
  })

  it('ANDs every contribution', () => {
    const yes = c('yes', { match: () => true })
    const no = c('no', { match: () => false })

    expect(applySessionFilters(session, [yes, yes])).toBe(true)
    expect(applySessionFilters(session, [yes, no])).toBe(false)
    expect(applySessionFilters(session, [no, yes])).toBe(false)
  })

  it('passes the session to match', () => {
    const match = vi.fn(() => true)

    applySessionFilters(session, [c('spy', { match })])
    expect(match).toHaveBeenCalledWith(session)
  })

  it('fails open on a throwing match and warns once per contribution id', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    const boom = c('boom-throws', {
      match: () => {
        throw new Error('nope')
      }
    })

    expect(applySessionFilters(session, [boom])).toBe(true)
    expect(applySessionFilters(session, [boom])).toBe(true)
    expect(applySessionFilters(session, [boom, c('no', { match: () => false })])).toBe(false)
    expect(warn).toHaveBeenCalledTimes(1)
  })

  it('fails open on a non-function match or missing data, warning once per id', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    expect(applySessionFilters(session, [c('bad-match', { match: 'x' }), c('no-data', undefined)])).toBe(true)
    expect(applySessionFilters(session, [c('bad-match', { match: 'x' }), c('no-data', undefined)])).toBe(true)
    expect(warn).toHaveBeenCalledTimes(2)
  })
})

describe('sessionHasOpenCards', () => {
  it('is false with no open cards', () => {
    expect(sessionHasOpenCards(session)).toBe(false)
    expect(sessionHasOpenCards({ ...session, kanban_active: 0, needs_you: [] })).toBe(false)
  })

  it.each([['kanban_active'], ['kanban_blocked'], ['kanban_held'], ['kanban_parked']] as const)('is true for %s', key => {
    expect(sessionHasOpenCards({ ...session, [key]: 1 })).toBe(true)
  })

  it('is true when needs_you is non-empty', () => {
    expect(sessionHasOpenCards({ ...session, needs_you: [{ id: 'x' }] as never })).toBe(true)
  })
})

describe('onClear', () => {
  it('runs every contribution onClear on clear/reset, surviving a throwing one', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})

    const first = vi.fn(() => {
      throw new Error('x')
    })

    const second = vi.fn()

    const dispose = registry.registerMany([
      { area: SESSION_FILTER_AREA, data: { match: () => true, onClear: first }, id: 'a' },
      { area: SESSION_FILTER_AREA, data: { match: () => true, onClear: second }, id: 'b' },
      { area: SESSION_FILTER_AREA, data: { match: () => true }, id: 'c' }
    ])

    clearSessionFilterContributions()
    expect(first).toHaveBeenCalledTimes(1)
    expect(second).toHaveBeenCalledTimes(1)

    resetSidebarView()
    expect(second).toHaveBeenCalledTimes(2)
    dispose()
  })
})
