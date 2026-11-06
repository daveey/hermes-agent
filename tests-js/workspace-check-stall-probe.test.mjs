import { mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { expect, test } from 'vitest'

import {
  SEVERE_STALL_MS,
  parseEvents,
  parsePressure,
  readCounters,
  startStallProbe,
  summarize,
} from '../.github/scripts/workspace-check-stall-probe.mjs'

const PSI = 'some avg10=0.00 avg60=1.20 avg300=0.50 total=2500000\nfull avg10=0.00 avg60=0.10 avg300=0.05 total=400000\n'

test('PSI totals are read as microseconds, and a missing or empty file is null', () => {
  expect(parsePressure(PSI)).toEqual({ some: 2500000, full: 400000 })
  expect(parsePressure('some avg10=0.00 avg60=0.00 avg300=0.00 total=7\n')).toEqual({ some: 7, full: null })
  expect(parsePressure(null)).toBeNull()
  expect(parsePressure('')).toBeNull()
})

test('memory.events counters are read by name', () => {
  expect(parseEvents('low 0\nhigh 4\nmax 12\noom 0\noom_kill 0\n')).toMatchObject({ high: 4, max: 12 })
  expect(parseEvents(null)).toEqual({})
})

test('the summary reports stall time per resource as the difference over the run', () => {
  const files = (io, memory, high) => (f) =>
    ({
      'io.pressure': `some avg10=0 avg60=0 avg300=0 total=${io}\nfull avg10=0 avg60=0 avg300=0 total=${io / 2}\n`,
      'memory.pressure': `some avg10=0 avg60=0 avg300=0 total=${memory}\nfull avg10=0 avg60=0 avg300=0 total=${memory}\n`,
      'memory.events': `high ${high}\nmax 0\n`,
    })[f] ?? null
  const before = readCounters(files(1_000_000, 0, 1))
  const after = readCounters(files(31_000_000, 2_000_000, 4))
  const samples = [
    { at: 1, fsyncMs: 12, lagMs: 1, running: [] },
    { at: 2, fsyncMs: 31_400, lagMs: 30_000, running: ['apps/desktop :: check:test:ui'] },
    { at: 3, fsyncMs: 1_200, lagMs: 0, running: [] },
  ]
  const s = summarize(samples, before, after)
  expect(s).toMatchObject({
    samples: 3,
    maxFsyncMs: 31_400,
    maxLagMs: 30_000,
    stalls: 2,
    severeStalls: 1,
    stalledMs: { ioSome: 30_000, ioFull: 15_000, memorySome: 2_000, memoryFull: 2_000, cpuSome: null },
    events: { high: 3, max: 0 },
  })
  expect(s.worst.map((w) => w.at)).toEqual([2, 3])
})

test('a runner without a cgroup gives null stall times, never zero', () => {
  const none = readCounters(() => null)
  expect(summarize([], none, none).stalledMs).toEqual({
    ioSome: null,
    ioFull: null,
    memorySome: null,
    memoryFull: null,
    cpuSome: null,
  })
})

test('the probe reports a slow write with the checks that were running, and stops sampling on stop()', async () => {
  let clock = 0
  const stalls = []
  const probe = startStallProbe({
    dir: '/unused',
    intervalMs: 1,
    now: () => clock,
    running: () => ['ui-tui :: check'],
    fsyncOnce: async () => {
      clock += SEVERE_STALL_MS + 1
    },
    onStall: (s) => stalls.push(s),
  })
  await new Promise((r) => setTimeout(r, 40))
  const samples = await probe.stop()
  const stoppedCount = samples.length
  expect(stoppedCount).toBeGreaterThan(0)
  expect(stalls.length).toBe(samples.length)
  expect(stalls[0]).toMatchObject({ fsyncMs: SEVERE_STALL_MS + 1, running: ['ui-tui :: check'] })
  await new Promise((r) => setTimeout(r, 20))
  expect(await probe.stop()).toHaveLength(stoppedCount)
})

test('a write that fails is skipped, not recorded as a fast disk', async () => {
  const probe = startStallProbe({
    dir: '/unused',
    intervalMs: 1,
    fsyncOnce: async () => {
      throw new Error('ENOSPC')
    },
  })
  await new Promise((r) => setTimeout(r, 20))
  expect(await probe.stop()).toEqual([])
})

test('the real write+fsync path measures the temp dir and leaves nothing behind', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'stall-probe-'))
  /** @type {ReturnType<typeof startStallProbe> | undefined} */
  let probe
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let deadline
  try {
    // Wait for three completed real write+fsyncs, not a fixed wall-clock
    // window: on a loaded runner (or Termux storage) one fsync can take
    // longer than 75 ms, and a 150 ms window then holds only two samples.
    // The probe calls `running()` once per completed write, as it records it.
    let completed = 0
    /** @type {() => void} */
    let threeDone = () => {}
    const three = new Promise((resolve, reject) => {
      threeDone = resolve
      // Reject here, before Vitest's 30s timeout, so finally can stop the
      // probe and remove its directory even if no successful samples arrive.
      deadline = setTimeout(() => reject(new Error(`Only ${completed}/3 fsync samples completed in 10s`)), 10_000)
    })
    probe = startStallProbe({
      dir,
      intervalMs: 5,
      running: () => {
        if (++completed === 3) threeDone()
        return []
      },
    })
    await three
    const samples = await probe.stop()
    expect(samples.length).toBeGreaterThan(2)
    expect(samples.every((s) => s.fsyncMs >= 0 && s.fsyncMs < SEVERE_STALL_MS)).toBe(true)
    expect(readdirSync(dir)).toEqual([])
  } finally {
    clearTimeout(deadline)
    try {
      await probe?.stop()
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  }
})
