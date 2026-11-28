import { describe, expect, it } from 'vitest'

import { formatWaitDuration, ORANGE_OVER, waitReadout } from './wait-eta'

describe('formatWaitDuration (largest unit)', () => {
  it.each([
    [30, '30s'],
    [0, '0s'],
    [12 * 60, '12m'],
    [3.4 * 3600, '3.4h'],
    [14 * 3600, '14h'],
    [2 * 86400, '2d'],
    [1.5 * 86400, '1.5d'],
    [16 * 60, '16m'],
    [3600, '1h'],
    [9.96 * 3600, '10h'],
    [-5, '0s']
  ])('%d s → %s', (seconds, text) => {
    expect(formatWaitDuration(seconds)).toBe(text)
  })

  it('shows an over-estimate as +<over>', () => {
    // 41 min waited against 25 expected: 16 min over
    expect(waitReadout({ expected_s: 25 * 60, wait_kind: 'ci', waited_since: 1000 }, 1000 + 41 * 60)?.left).toBe('+16m')
  })
})

describe('waitReadout', () => {
  const now = 100_000

  it('counts left down from the client clock', () => {
    const eta = {
      estimate_source: 'median of last 20 CI runs (acme/widgets)',
      expected_s: 25 * 60,
      wait_kind: 'ci',
      waited_since: now - 12 * 60
    }
    const early = waitReadout(eta, now)
    const later = waitReadout(eta, now + 60)

    expect([early?.waited, early?.expected, early?.left, early?.over]).toEqual(['12m', '25m', '13m', false])
    expect(later?.left).toBe('12m')
    expect(early?.title).toBe(
      'Waiting on CI · waited 12m · expected 25m (median of last 20 CI runs (acme/widgets)) · 13m left'
    )
  })

  it('names the kind in the title: CI, deploy, timer', () => {
    const at = { expected_s: 600, waited_since: now - 60 }

    expect(waitReadout({ ...at, wait_kind: 'deploy' }, now)?.title).toMatch(/^Waiting on deploy · /)
    expect(waitReadout({ ...at, wait_kind: 'clock' }, now)?.title).toMatch(/^Waiting on timer · /)
  })

  it('past the estimate reads +over and says so in the title', () => {
    const readout = waitReadout(
      { estimate_source: 'wakes 14:30', expected_s: 25 * 60, wait_kind: 'clock', waited_since: now - 41 * 60 },
      now
    )

    expect([readout?.left, readout?.over]).toEqual(['+16m', true])
    expect(readout?.title).toBe('Waiting on timer · waited 41m · expected 25m (wakes 14:30) · 16m over')
    expect(ORANGE_OVER).toBe('#f0883e')
  })

  it('without an estimate reads <waited> / ? and a dash', () => {
    const readout = waitReadout({ expected_s: null, wait_kind: 'ci', waited_since: now - 3.4 * 3600 }, now)

    expect([readout?.waited, readout?.expected, readout?.left, readout?.unknown]).toEqual(['3.4h', null, '—', true])
  })

  it('claims nothing without a park time or a known kind', () => {
    expect(waitReadout(null, now)).toBeNull()
    expect(waitReadout({ expected_s: 60, wait_kind: 'ci' }, now)).toBeNull()
    expect(waitReadout({ expected_s: 60, wait_kind: 'external', waited_since: 1 }, now)).toBeNull()
  })
})
