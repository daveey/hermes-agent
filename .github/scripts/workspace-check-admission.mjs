// Which workspace checks run-workspace-checks.mjs may start together.
//
// Admission is by CPU and by memory. The runner slots are 4 CPUs and 8 GiB
// with no swap; four apps/desktop units started together (tsc, the vite build
// inside `pack`, two vitest pools) went past 8 GiB and the kernel SIGKILLed
// tsc and vite (runs 41009740205, 41016178444). A package declares the
// measured peak of each heavy unit in its package.json `checkMemoryMiB` map;
// a unit starts only when its peak fits in what the running units leave of
// the memory budget. A unit bigger than the whole budget still runs, alone.
// The apps/desktop values are each unit's cgroup memory.peak measured alone
// in a 4-CPU / 8 GiB / no-swap scope, plus ~10% (ui vitest 3.1 GiB,
// tsc + eslint 3.0, pack + packaged-app tests 2.4).
//
// Pure functions only, so tests import them without running the checks.

const MIB = 2 ** 20

// A unit with no declared peak: the light checks (tsc + eslint of a small
// package, a short vitest run) measure 0.4-1.1 GiB.
export const DEFAULT_UNIT_MIB = 1024
// Left out of the budget for the runner itself (Runner.Worker, Xvfb, the
// npm wrappers) and for page cache the cgroup also charges.
export const RESERVED_MIB = 1024

/**
 * @typedef {{pkg: string, script: string, memoryMiB: number}} Unit
 * @typedef {{location: string, path?: string, scripts?: Record<string,string>}} WorkspacePkg
 */

/**
 * The units of every workspace, each with its declared memory peak.
 *
 * @param {WorkspacePkg[]} pkgs
 * @param {(pkg: WorkspacePkg) => Record<string, number> | undefined} readMemory
 * @returns {Unit[]}
 */
export function unitsOf(pkgs, readMemory) {
  /** @type {Unit[]} */
  const units = []
  for (const pkg of pkgs) {
    const scripts = pkg.scripts || {}
    const subs = Object.keys(scripts).filter((s) => /^check:.+$/.test(s))
    const names = subs.length > 0 ? subs : scripts.check ? ['check'] : []
    const memory = readMemory(pkg) || {}
    for (const declared of Object.keys(memory)) {
      // A renamed check would otherwise drop back to the default peak and
      // be admitted next to the units it was declared to keep out.
      if (!names.includes(declared)) {
        throw new Error(`${pkg.location}: checkMemoryMiB names "${declared}", which is not a check unit of this package`)
      }
      if (!Number.isInteger(memory[declared]) || memory[declared] <= 0) {
        throw new Error(`${pkg.location}: checkMemoryMiB["${declared}"] must be a positive integer of MiB`)
      }
    }
    for (const script of names) {
      units.push({ pkg: pkg.location, script, memoryMiB: memory[script] ?? DEFAULT_UNIT_MIB })
    }
  }
  return units
}

/**
 * The memory the checks may use, in MiB: the cgroup limit when there is one
 * (a runner container's `--memory`), else physical memory, less the reserve.
 *
 * @param {{constrained?: number, total: number}} machine bytes
 */
export function memoryBudgetMiB({ constrained, total }) {
  // constrainedMemory() is 0 or 2^64-1 with no limit set.
  const limit = constrained && constrained > 0 && constrained < total ? constrained : total
  return Math.max(1, Math.floor(limit / MIB) - RESERVED_MIB)
}

/**
 * Start each unit when a CPU slot is free and its memory peak fits in the
 * budget the running units leave. Queue order is kept, but a later unit that
 * fits starts before an earlier one that doesn't. A unit larger than the
 * whole budget starts only when nothing else runs.
 *
 * @template R
 * @param {Unit[]} units
 * @param {{concurrency: number, budgetMiB: number, run: (unit: Unit) => Promise<R>, onDone?: (r: R) => void}} opts
 * @returns {Promise<R[]>}
 */
export function schedule(units, { concurrency, budgetMiB, run, onDone }) {
  const pending = [...units]
  /** @type {R[]} */
  const results = []
  let running = 0
  let usedMiB = 0
  return new Promise((resolve, reject) => {
    function pump() {
      for (let i = 0; i < pending.length && running < concurrency; ) {
        const unit = pending[i]
        const cost = Math.min(unit.memoryMiB, budgetMiB)
        if (running > 0 && usedMiB + cost > budgetMiB) {
          i++
          continue
        }
        pending.splice(i, 1)
        running++
        usedMiB += cost
        run(unit)
          .then((res) => {
            running--
            usedMiB -= cost
            results.push(res)
            onDone?.(res)
            pump()
          })
          .catch(reject)
      }
      if (pending.length === 0 && running === 0) resolve(results)
    }
    pump()
  })
}

/**
 * The `oom_kill` count of a cgroup v2 `memory.events` file, or null.
 *
 * @param {string | null} events
 */
export function oomKills(events) {
  const n = events?.match(/^oom_kill (\d+)$/m)?.[1]
  return n === undefined ? null : Number(n)
}
