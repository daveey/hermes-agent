import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  isTileCachePaint,
  markOpenClick,
  markOpenPaint,
  markOpenReconciled,
  markOpenStage,
  markOpenStart,
  type OpenTimingRecord,
  parseOpenTimingLine,
  resetOpenTimings,
  tileCachePaintProof
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

  // [daveey] A sidebar click on a chat already on screen as a tile only fronts the tile: main never resumes it,
  // so the click is not a main-pane open. It used to dangle and log `outcome:"abandoned"` with no stages when the
  // next open started (desktop.log 11-10 10:37:04, 97 s later), which read as a failed open of that session.
  it('a click that never reached resumeSession (focused tile, refused offline) is not logged when superseded', () => {
    markOpenClick('focused-tile', 'hermes-hub')
    markOpenClick('next')

    expect(records()).toEqual([])
  })

  it('a click that entered resumeSession and is superseded is still logged abandoned', () => {
    markOpenClick('slow', 'hermes-hub')
    markOpenStart('slow', 'hermes-hub')
    markOpenClick('next')

    expect(records()).toEqual([expect.objectContaining({ id: 'slow', outcome: 'abandoned', startedBy: 'click' })])
  })
})

describe('tile cache-paint proof gate', () => {
  const id = '20261018_114650_7ed013'

  // The exact two lines the 11-10 live proof collected: sidebar main-pane opens, no surface.
  const observed = [
    '[open-timing] {"clickAt":33139333.099999905,"connection":"hermes-hub","id":"20261018_114650_7ed013","startedBy":"click","outcome":"abandoned","reconciledMs":97315}',
    '[open-timing] {"clickAt":33333456.19999993,"connection":"hermes-hub","id":"20261018_114650_7ed013","startedBy":"click","stages":{"owner":68,"gateway":71,"rest":335},"paint":"rest","paintMs":336,"paintRows":13,"outcome":"rest","reconciledMs":336,"changed":true}'
  ]

  it('main-pane records cannot satisfy the tile proof, whatever they painted', () => {
    expect(observed.map(line => parseOpenTimingLine(line)?.id)).toEqual([id, id])
    expect(tileCachePaintProof(observed, id)).toBeNull()
    expect(isTileCachePaint({ clickAt: 1, id, outcome: 'rest', paint: 'cache', startedBy: 'click' })).toBe(false)
  })

  it('abandoned, failed, never-painted and REST-painted tile opens are not success', () => {
    const tile = { clickAt: 1, id, startedBy: 'resume' as const, surface: 'tile' as const }

    expect(isTileCachePaint({ ...tile, outcome: 'abandoned', paint: 'cache' })).toBe(false)
    expect(isTileCachePaint({ ...tile, outcome: 'failed', paint: 'cache' })).toBe(false)
    expect(isTileCachePaint({ ...tile, outcome: 'abandoned' })).toBe(false)
    expect(isTileCachePaint({ ...tile, outcome: 'failed' })).toBe(false)
    expect(isTileCachePaint({ ...tile, outcome: 'rest', paint: 'rest' })).toBe(false)
    expect(isTileCachePaint({ ...tile, outcome: 'rest', paint: 'cache' })).toBe(true)
  })

  it('finds the tile cache paint for the requested id only, ignoring other ids and non-timing lines', () => {
    const hit = `[open-timing] {"clickAt":5,"id":"${id}","startedBy":"resume","surface":"tile","paint":"cache","paintMs":4,"paintRows":13,"outcome":"rest","reconciledMs":300,"changed":false}`
    const otherId = hit.replace(id, 'someone-else')

    expect(tileCachePaintProof([...observed, otherId, 'noise [open-timing] {not json', hit], id)).toMatchObject({
      id,
      paint: 'cache',
      surface: 'tile'
    })
    expect(tileCachePaintProof([...observed, otherId], id)).toBeNull()
  })
})
