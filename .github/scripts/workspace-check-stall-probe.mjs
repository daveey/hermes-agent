// A stall probe for run-workspace-checks.mjs.
//
// Main CI run 41087044980 failed on four tests that each sat for 24-34 s
// (a git init, a scripted ssh close, a bash spawn, a store rewrite) while the
// same sha passed twice. Total test time was the same in all three runs, so
// the runner did not run slower. Single operations froze, in passing runs too
// (`ensureGitRepo` 26 s, checkout-handoff 35 s). Nothing in the job log says
// whether the freeze is the disk (fsync), memory reclaim at the cgroup limit,
// or CPU. This probe measures all three on the runner itself:
//
//   - every second it writes and fsyncs a small file in the temp directory the
//     tests use, and records how long that took;
//   - it reads the cgroup's PSI files (io, memory, cpu) and memory.events at
//     the start and the end, so the summary carries the time tasks spent
//     stalled on each resource during this run.
//
// Pure functions and an injectable fsync, so tests run without a cgroup.

import { open, unlink } from 'node:fs/promises'
import { join } from 'node:path'

/** A probe write+fsync slower than this is a stall worth naming. */
export const STALL_MS = 1000
/** ... and slower than this is long enough to time out a 5 s test on its own. */
export const SEVERE_STALL_MS = 5000

/**
 * The cumulative stall time of a cgroup v2 PSI file, in microseconds.
 * `full` is absent on cpu.pressure of older kernels.
 *
 * @param {string | null} text
 * @returns {{some: number, full: number | null} | null}
 */
export function parsePressure(text) {
  if (!text) return null
  const total = (/** @type {string} */ kind) => {
    const n = text.match(new RegExp(`^${kind} .*\\btotal=(\\d+)`, 'm'))?.[1]
    return n === undefined ? null : Number(n)
  }
  const some = total('some')
  return some === null ? null : { some, full: total('full') }
}

/**
 * The counters of a cgroup v2 `memory.events` file.
 *
 * @param {string | null} text
 * @returns {Record<string, number>}
 */
export function parseEvents(text) {
  /** @type {Record<string, number>} */
  const out = {}
  for (const line of (text ?? '').split('\n')) {
    const [key, value] = line.trim().split(/\s+/)
    if (key && /^\d+$/.test(value ?? '')) out[key] = Number(value)
  }
  return out
}

/**
 * One reading of everything the summary compares.
 *
 * @param {(file: string) => string | null} read reads a file of the job's cgroup
 */
export function readCounters(read) {
  return {
    io: parsePressure(read('io.pressure')),
    memory: parsePressure(read('memory.pressure')),
    cpu: parsePressure(read('cpu.pressure')),
    events: parseEvents(read('memory.events')),
  }
}

/** @typedef {{at: number, fsyncMs: number, lagMs: number, running: string[]}} Sample */

/**
 * @param {Sample[]} samples
 * @param {ReturnType<typeof readCounters>} before
 * @param {ReturnType<typeof readCounters>} after
 */
export function summarize(samples, before, after) {
  const sorted = [...samples].sort((a, b) => b.fsyncMs - a.fsyncMs)
  const delta = (/** @type {'io'|'memory'|'cpu'} */ k, /** @type {'some'|'full'} */ f) => {
    const a = after[k]?.[f]
    const b = before[k]?.[f]
    return a == null || b == null ? null : Math.round((a - b) / 1000)
  }
  const event = (/** @type {string} */ k) =>
    k in after.events && k in before.events ? after.events[k] - before.events[k] : null
  return {
    samples: samples.length,
    maxFsyncMs: sorted[0] ? Math.round(sorted[0].fsyncMs) : null,
    maxLagMs: samples.reduce((m, s) => Math.max(m, Math.round(s.lagMs)), 0),
    stalls: samples.filter((s) => s.fsyncMs >= STALL_MS).length,
    severeStalls: samples.filter((s) => s.fsyncMs >= SEVERE_STALL_MS).length,
    worst: sorted.slice(0, 5).filter((s) => s.fsyncMs >= STALL_MS),
    // Milliseconds any task / all tasks of the cgroup spent stalled.
    stalledMs: {
      ioSome: delta('io', 'some'),
      ioFull: delta('io', 'full'),
      memorySome: delta('memory', 'some'),
      memoryFull: delta('memory', 'full'),
      cpuSome: delta('cpu', 'some'),
    },
    // Times the cgroup crossed memory.high / hit memory.max during this run.
    events: { high: event('high'), max: event('max') },
  }
}

/** @param {string} dir */
async function defaultFsync(dir) {
  const path = join(dir, `.stall-probe-${process.pid}`)
  const handle = await open(path, 'w')
  try {
    await handle.write(Buffer.alloc(4096, 1))
    await handle.sync()
  } finally {
    await handle.close()
    await unlink(path).catch(() => {})
  }
}

/**
 * Start sampling. `stop()` ends it and returns the samples.
 *
 * @param {{dir: string, intervalMs?: number, running?: () => string[], onStall?: (s: Sample) => void,
 *   fsyncOnce?: (dir: string) => Promise<void>, now?: () => number}} opts
 * @returns {{stop: () => Promise<Sample[]>}}
 */
export function startStallProbe({
  dir,
  intervalMs = 1000,
  running = () => [],
  onStall,
  fsyncOnce = defaultFsync,
  now = () => performance.now(),
}) {
  /** @type {Sample[]} */
  const samples = []
  let stopped = false
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let timer
  /** @type {Promise<void>} */
  let loop = Promise.resolve()

  function schedule() {
    const due = now() + intervalMs
    timer = setTimeout(() => {
      loop = tick(due)
    }, intervalMs)
    // The probe must never keep the process alive.
    timer.unref?.()
  }

  /** @param {number} due */
  async function tick(due) {
    const started = now()
    // How late the timer fired: the event loop of this process was stuck.
    const lagMs = Math.max(0, started - due)
    try {
      await fsyncOnce(dir)
    } catch {
      // A probe that cannot write says nothing about the disk; skip the sample.
      if (!stopped) schedule()
      return
    }
    /** @type {Sample} */
    const sample = { at: Date.now(), fsyncMs: now() - started, lagMs, running: running() }
    samples.push(sample)
    if (sample.fsyncMs >= SEVERE_STALL_MS) onStall?.(sample)
    if (!stopped) schedule()
  }

  schedule()
  return {
    async stop() {
      stopped = true
      clearTimeout(timer)
      await loop
      return samples
    },
  }
}
