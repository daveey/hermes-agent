import { describe, expect, it } from 'vitest'

import { en } from '@/i18n/en'

import {
  describeBackgroundUpdate,
  formatDuration,
  mapUpdateProgress,
  type UpdateProgressResponse
} from './background-update'

const copy = en.shell.statusbar.backgroundUpdate
const FETCHED = 1_000_000

const wire = (over: Partial<NonNullable<UpdateProgressResponse['progress']>> = {}): UpdateProgressResponse => ({
  progress: { state: 'draining', due_at: 1, state_since: 1, elapsed_s: 30, cap_s: 60, waiting_on: [], ...over }
})

describe('mapUpdateProgress', () => {
  it('keeps the four update states and drops everything unusable', () => {
    expect(mapUpdateProgress(wire(), FETCHED)).toEqual({
      state: 'draining',
      elapsedS: 30,
      capS: 60,
      waitingOn: [],
      fetchedAt: FETCHED
    })
    expect(mapUpdateProgress({ progress: null }, FETCHED)).toBeNull()
    expect(mapUpdateProgress(undefined, FETCHED)).toBeNull()
    expect(mapUpdateProgress(wire({ state: 'idle' }), FETCHED)).toBeNull()
    expect(mapUpdateProgress(wire({ elapsed_s: Number.NaN }), FETCHED)).toBeNull()
    expect(mapUpdateProgress(wire({ cap_s: 0 }), FETCHED)?.capS).toBeNull()
  })
})

describe('describeBackgroundUpdate', () => {
  it('fills the bar toward the drainer cap, advancing on the local clock since the fetch', () => {
    const view = describeBackgroundUpdate(mapUpdateProgress(wire(), FETCHED), FETCHED + 15_000, copy)

    expect(view).toMatchObject({ busy: true, suffix: 'updating', bar: { fraction: 0.75 } })
    expect(view?.tooltip).toContain(copy.tooltipDraining('45s'))
    expect(view?.tooltip).not.toContain('Past its')
  })

  it('past the cap: sweeps instead of a full bar and says what it is still waiting on', () => {
    const waiting = ['default:a', 'default:b', 'default:c', 'default:d', 'default:e', 'default:f']

    const view = describeBackgroundUpdate(
      mapUpdateProgress(wire({ elapsed_s: 701, waiting_on: waiting }), FETCHED),
      FETCHED,
      copy
    )

    expect(view?.bar).toEqual({ fraction: null })
    expect(view?.tooltip).toContain(copy.tooltipDraining('12m'))
    expect(view?.tooltip).toContain(copy.tooltipPastCap('1m'))
    expect(view?.tooltip).toContain(copy.waitingOn('default:a, default:b, default:c, default:d +2'))
  })

  it('installing is busy with an indeterminate bar; blocked and batching are quiet', () => {
    expect(
      describeBackgroundUpdate(mapUpdateProgress(wire({ state: 'applying', elapsed_s: 720 }), FETCHED), FETCHED, copy)
    ).toEqual({ busy: true, suffix: 'updating', tooltip: copy.tooltipApplying('12m'), bar: { fraction: null } })

    const blocked = describeBackgroundUpdate(
      mapUpdateProgress(wire({ state: 'blocked', waiting_on: ['hold x'] }), FETCHED),
      FETCHED,
      copy
    )

    expect(blocked).toMatchObject({ busy: false, suffix: copy.blocked, bar: null })
    expect(blocked?.tooltip).toContain(copy.waitingOn('hold x'))

    const batching = describeBackgroundUpdate(
      mapUpdateProgress(wire({ state: 'batching', elapsed_s: -1860 }), FETCHED),
      FETCHED,
      copy
    )

    expect(batching).toMatchObject({ busy: false, suffix: copy.dueIn('31m'), bar: null })
    // A batch already due shows nothing until the drainer publishes `draining`.
    expect(
      describeBackgroundUpdate(
        mapUpdateProgress(wire({ state: 'batching', elapsed_s: -5 }), FETCHED),
        FETCHED + 10_000,
        copy
      )
    ).toBeNull()
  })

  it('formats durations the way the tooltip reads them', () => {
    expect([formatDuration(0), formatDuration(59), formatDuration(701), formatDuration(3900)]).toEqual([
      '0s',
      '59s',
      '12m',
      '1h 5m'
    ])
  })
})
