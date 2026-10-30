// Run every workspace check at the same time and report all failures.
//
// The unit of work is a CHECK, and not a workspace. A package that declares
// `check:*` sub-scripts gives one unit for each sub-script. A package with a
// plain `check` gives that. This is the same selection rule the old CI matrix
// used, so the set of commands is unchanged. Only the schedule is different.
//
// This is not `npm run --ws check`, because that command is serial and stops
// at the first workspace that fails. This runs every unit and fails at the
// end with the full list.
//
// The output of each unit goes to a buffer and prints on completion inside a
// group that collapses. Children that write to one stdout together interleave
// their lines, and a failure is then hard to read.
//
// Admission is by CPU and by memory. The runner slots are 4 CPUs and 8 GiB
// with no swap; four apps/desktop units started together (tsc, the vite build
// inside `pack`, two vitest pools) went past 8 GiB and the cgroup OOM killer
// SIGKILLed tsc and vite (runs 41009740205, 41016178444). A package declares
// the measured peak of each heavy unit in its package.json
// `checkMemoryMiB` map; a unit starts only when its peak fits in what the
// running units leave of the memory budget. A unit bigger than the whole
// budget still runs, alone. The apps/desktop values are each unit's cgroup
// memory.peak measured alone in a 4-CPU / 8 GiB / no-swap scope, plus ~10%
// (ui vitest 3.1 GiB, tsc + eslint 3.0, pack + packaged-app tests 2.4).
//
// This also runs on a laptop: `node .github/scripts/run-workspace-checks.mjs`.
// `--concurrency N` sets the CPU limit. `--list` prints the units and exits.

import { execFileSync, spawn } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { availableParallelism, totalmem } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const IS_CI = Boolean(process.env.GITHUB_ACTIONS)
const NPM = process.platform === 'win32' ? 'npm.cmd' : 'npm'
const MIB = 2 ** 20

// A unit with no declared peak: the light checks (tsc + eslint of a small
// package, a short vitest run) measure 0.4-1.1 GiB.
export const DEFAULT_UNIT_MIB = 1024
// Left out of the budget for the runner itself (Runner.Worker, Xvfb, this
// process) and for page cache the cgroup also charges.
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

/** @returns {Unit[]} */
function discoverUnits() {
  const raw = execFileSync(NPM, ['query', '.workspace'], {
    encoding: 'utf-8',
    shell: process.platform === 'win32',
  })
  // `npm query` returns the standard manifest fields only, so the memory map
  // is read from the package.json itself.
  return unitsOf(JSON.parse(raw), (pkg) =>
    JSON.parse(readFileSync(join(pkg.path || pkg.location, 'package.json'), 'utf-8')).checkMemoryMiB,
  )
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
        run(unit).then((res) => {
          running--
          usedMiB -= cost
          results.push(res)
          onDone?.(res)
          pump()
        }, reject)
      }
      if (pending.length === 0 && running === 0) resolve(results)
    }
    pump()
  })
}

/** The job cgroup's memory accounting (Linux cgroup v2), or null. */
function cgroupMemory() {
  try {
    const rel = readFileSync('/proc/self/cgroup', 'utf-8').match(/^0::(.*)$/m)?.[1]
    if (rel === undefined) return null
    const dir = join('/sys/fs/cgroup', rel)
    const read = (/** @type {string} */ f) => {
      try {
        return readFileSync(join(dir, f), 'utf-8').trim()
      } catch {
        return null
      }
    }
    const oomKill = read('memory.events')?.match(/^oom_kill (\d+)$/m)?.[1]
    return { max: read('memory.max'), peak: read('memory.peak'), oomKill: oomKill === undefined ? null : Number(oomKill) }
  } catch {
    return null
  }
}

/** @param {Unit} unit */
function runUnit(unit) {
  return new Promise((resolve) => {
    const started = Date.now()
    const child = spawn(NPM, ['run', '--prefix', unit.pkg, unit.script], {
      // Buffer, and do not inherit. Children that share one stdout
      // interleave their lines, and a failure is then hard to read.
      stdio: ['ignore', 'pipe', 'pipe'],
      shell: process.platform === 'win32',
    })
    /** @type {Buffer[]} */
    const chunks = []
    child.stdout.on('data', (c) => chunks.push(c))
    child.stderr.on('data', (c) => chunks.push(c))
    child.on('error', (err) => {
      chunks.push(Buffer.from(`failed to spawn: ${err.message}\n`))
      resolve({ unit, code: 1, output: Buffer.concat(chunks).toString('utf-8'), ms: Date.now() - started })
    })
    child.on('close', (code) => {
      resolve({
        unit,
        code: code ?? 1,
        output: Buffer.concat(chunks).toString('utf-8'),
        ms: Date.now() - started,
      })
    })
  })
}

async function main() {
  const argv = process.argv.slice(2)
  const units = discoverUnits()

  if (units.length === 0) {
    console.error(
      '::error::No workspace package declares a check script — refusing to report green having run nothing.',
    )
    process.exit(1)
  }

  if (argv.includes('--list')) {
    for (const u of units) console.log(`${u.pkg} :: ${u.script}  (${u.memoryMiB} MiB)`)
    return
  }

  const flagIdx = argv.indexOf('--concurrency')
  const concurrency = Math.max(
    1,
    flagIdx !== -1 ? Number(argv[flagIdx + 1]) : Math.min(units.length, availableParallelism()),
  )
  const budgetMiB = memoryBudgetMiB({ constrained: process.constrainedMemory?.(), total: totalmem() })

  console.log(`running ${units.length} checks, up to ${concurrency} at a time within ${budgetMiB} MiB:`)
  for (const u of units) console.log(`  ${u.pkg} :: ${u.script}  (${u.memoryMiB} MiB)`)
  console.log('')

  /** @param {{unit: Unit, code: number, output: string, ms: number}} res */
  function report(res) {
    const label = `${res.unit.pkg} :: ${res.unit.script}`
    const secs = (res.ms / 1000).toFixed(1)
    const status = res.code === 0 ? 'PASS' : 'FAIL'
    if (IS_CI) console.log(`::group::${status} ${label} (${secs}s)`)
    else console.log(`----- ${status} ${label} (${secs}s) -----`)
    process.stdout.write(res.output.endsWith('\n') ? res.output : res.output + '\n')
    if (IS_CI) console.log('::endgroup::')
  }

  const results = await schedule(units, { concurrency, budgetMiB, run: runUnit, onDone: report })

  const failed = results.filter((r) => r.code !== 0)
  console.log('\n=== summary ===')
  for (const r of [...results].sort((a, b) => b.ms - a.ms)) {
    console.log(
      `  ${r.code === 0 ? 'pass' : 'FAIL'}  ${(r.ms / 1000).toFixed(1).padStart(6)}s  ${r.unit.pkg} :: ${r.unit.script}`,
    )
  }
  // A SIGKILLed check prints only "Killed". The cgroup's own counters say
  // whether the memory limit did it.
  const mem = cgroupMemory()
  if (mem) {
    console.log(`  cgroup memory: max=${mem.max} peak=${mem.peak ?? 'n/a'} oom_kill=${mem.oomKill ?? 'n/a'}`)
    if (mem.oomKill) {
      console.error(
        `::error::the cgroup OOM killer ran ${mem.oomKill} time(s): a check was killed for memory (limit ${mem.max}); fix checkMemoryMiB, not the check`,
      )
    }
  }

  if (failed.length > 0) {
    for (const r of failed) console.error(`::error::${r.unit.pkg} :: ${r.unit.script} failed`)
    console.error(`::error::${failed.length} of ${results.length} checks failed`)
    // Not process.exit(): it drops what stdout still buffers, which is the failing check's output.
    process.exitCode = 1
    return
  }
  console.log(`\nall ${results.length} checks passed`)
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main()
