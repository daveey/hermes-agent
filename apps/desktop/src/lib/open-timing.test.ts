import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  markOpenClick,
  markOpenPaint,
  markOpenReconciled,
  markOpenStage,
  markOpenStart,
  type OpenTimingRecord,
  resetOpenTimings
} from './open-timing'

const records = () => (globalThis as { __hermesOpenTimings?: OpenTimingRecord[] }).__hermesOpenTimings ?? []

afterEach(() => {
  resetOpenTimings()
  vi.useRealTimers()
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
})

describe('open timing log', () => {
  it('writes one line per open: click -> first paint -> stages -> reconciled', () => {
    vi.useFakeTimers({ toFake: ['performance'] })

    const logLine = vi.fn()

    ;(window as { hermesDesktop?: unknown }).hermesDesktop = { logLine }

    markOpenClick('s1', 'mac-mini')
    vi.advanceTimersByTime(5)
    markOpenStart('s1') // resumeSession entry keeps the click clock
    vi.advanceTimersByTime(10)
    markOpenPaint('s1', 'cache', 12)
    markOpenPaint('s1', 'rest', 40) // later paints do not move the first one
    vi.advanceTimersByTime(300)
    markOpenStage('s1', 'gateway')
    vi.advanceTimersByTime(200)
    markOpenReconciled('s1', 'rest', false)

    expect(records()).toEqual([
      expect.objectContaining({
        changed: false,
        connection: 'mac-mini',
        outcome: 'rest',
        paint: 'cache',
        paintMs: 15,
        paintRows: 12,
        reconciledMs: 515,
        stages: { gateway: 315 },
        startedBy: 'click'
      })
    ])
    expect(logLine).toHaveBeenCalledWith(expect.stringMatching(/^\[open-timing\] \{"/))
  })

  it('writes a superseded open as abandoned with its paint', () => {
    markOpenClick('a')
    markOpenPaint('a', 'cache', 3)
    markOpenClick('b')

    expect(records()).toEqual([expect.objectContaining({ id: 'a', outcome: 'abandoned', paint: 'cache' })])
  })

  it('a resume without a recent click starts its own clock', () => {
    markOpenStart('r1')
    markOpenReconciled('r1', 'asleep')

    expect(records()[0]).toMatchObject({ id: 'r1', outcome: 'asleep', startedBy: 'resume' })
  })
})
