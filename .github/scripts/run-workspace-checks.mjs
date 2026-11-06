// Run every workspace check at the same time and report all failures.
//
// The unit of work is a CHECK, and not a workspace. A package that declares
// `check:*` sub-scripts gives one unit for each sub-script. A package with a
// plain `check` gives that. This is the same selection rule the old CI matrix
// used, so the set of commands is unchanged. Only the schedule is different.
//
// This is not `pnpm -r run check`, because that command stops
// at the first workspace that fails. This runs every unit and fails at the
// end with the full list.
//
// The output of each unit goes to a buffer and prints on completion inside a
// group that collapses. Children that write to one stdout together interleave
// their lines, and a failure is then hard to read.
//
// Admission is by CPU and by memory: see workspace-check-admission.mjs.
//
// This also runs on a laptop: `node .github/scripts/run-workspace-checks.mjs`.
// `--concurrency N` sets the CPU limit. `--list` prints the units and exits.

import { execFileSync, spawn } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { availableParallelism, tmpdir, totalmem } from 'node:os'
import { join, relative } from 'node:path'

import { memoryBudgetMiB, oomKills, schedule, unitsOf } from './workspace-check-admission.mjs'
import { readCounters, startStallProbe, summarize } from './workspace-check-stall-probe.mjs'
import { temporaryMemoryMiB } from './workspace-check-temp-memory.mjs'

const IS_CI = Boolean(process.env.GITHUB_ACTIONS)
const PNPM = 'pnpm'

/** @typedef {import('./workspace-check-admission.mjs').Unit} Unit */

/** @returns {Unit[]} */
function discoverUnits() {
  const raw = execFileSync(PNPM, ['ls', '--recursive', '--depth', '-1', '--json'], {
    encoding: 'utf-8',
    shell: process.platform === 'win32',
  })
  /** @type {{path: string}[]} */
  const projects = JSON.parse(raw)
  const root = process.cwd()

  const packages = []
  for (const project of projects) {
    const location = relative(root, project.path).split('\\').join('/')
    // The root's `check` runs every workspace's check: it is not a unit.
    if (!location) continue
    packages.push({ ...JSON.parse(readFileSync(join(project.path, 'package.json'), 'utf-8')), location })
  }
  // pnpm's listing omits scripts and memory declarations; read both from manifests.
  return unitsOf(packages, (pkg) =>
    JSON.parse(readFileSync(join(root, pkg.location, 'package.json'), 'utf-8')).checkMemoryMiB,
  )
}

/** Reads one file of the job's cgroup (Linux cgroup v2), or null without a cgroup. */
function cgroupReader() {
  let rel
  try {
    rel = readFileSync('/proc/self/cgroup', 'utf-8').match(/^0::(.*)$/m)?.[1]
  } catch {
    return null
  }
  if (rel === undefined) return null
  return (/** @type {string} */ f) => {
    try {
      return readFileSync(join('/sys/fs/cgroup', rel, f), 'utf-8').trim()
    } catch {
      return null
    }
  }
}

/** The job cgroup's memory files, or null. */
function cgroupMemory() {
  const read = cgroupReader()
  if (!read) return null
  return { max: read('memory.max'), peak: read('memory.peak'), oomKill: oomKills(read('memory.events')) }
}

/** The filesystem the tests' temp directory lives on, from /proc/self/mounts. */
function tmpMount() {
  const dir = tmpdir()
  try {
    const mount = readFileSync('/proc/self/mounts', 'utf-8')
      .split('\n')
      .map((l) => l.split(' '))
      .filter((m) => m[1] && (m[1] === '/' || dir === m[1] || dir.startsWith(`${m[1]}/`)))
      .sort((a, b) => b[1].length - a[1].length)[0]
    return mount ? `${dir} on ${mount[1]} (${mount[2]}, ${mount[3]})` : dir
  } catch {
    return dir
  }
}

/** @param {Unit} unit */
function runUnit(unit) {
  return new Promise((resolve) => {
    const started = Date.now()
    const child = spawn(PNPM, ['--dir', unit.pkg, 'run', unit.script], {
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
  const budgetMiB = memoryBudgetMiB({ constrained: process.constrainedMemory?.(), total: totalmem(), temporaryMiB: temporaryMemoryMiB(tmpdir()) })
  // A runner container can serve many jobs, so its counters start where the
  // last job left them; this run's OOM kills are the difference.
  const oomBefore = cgroupMemory()?.oomKill ?? null

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

  // Single operations freeze for 20-35 s on the CI runners and time tests
  // out (run 41087044980). This records whether the disk, memory reclaim or
  // the CPU did it: see workspace-check-stall-probe.mjs.
  const read = cgroupReader() ?? (() => null)
  const countersBefore = readCounters(read)
  /** @type {Set<string>} */
  const live = new Set()
  const probe = startStallProbe({
    dir: tmpdir(),
    running: () => [...live],
    onStall: (s) =>
      console.log(
        `::warning::stall probe: temp-dir write+fsync took ${Math.round(s.fsyncMs)} ms while ${s.running.join(', ') || 'nothing'} ran`,
      ),
  })
  // Keep measuring the backing checkout disk after fixtures move to tmpfs.
  // This distinguishes isolation from repair of the EBS/overlay layer.
  const diskProbe = IS_CI ? startStallProbe({
    dir: process.cwd(),
    running: () => [...live],
    onStall: (s) => console.log(`::warning::checkout disk write+fsync took ${Math.round(s.fsyncMs)} ms`),
  }) : null
  /** @param {Unit} unit */
  const runTracked = async (unit) => {
    const label = `${unit.pkg} :: ${unit.script}`
    live.add(label)
    try {
      return await runUnit(unit)
    } finally {
      live.delete(label)
    }
  }

  const results = await schedule(units, { concurrency, budgetMiB, run: runTracked, onDone: report })
  const stall = summarize(await probe.stop(), countersBefore, readCounters(read))
  if (diskProbe) {
    const disk = summarize(await diskProbe.stop(), countersBefore, readCounters(read))
    console.log(`  checkout disk probe: ${disk.samples} samples, max fsync ${disk.maxFsyncMs} ms, ${disk.severeStalls} >= 5 s`)
  }

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
    const kills = mem.oomKill === null || oomBefore === null ? null : mem.oomKill - oomBefore
    console.log(
      `  cgroup memory: max=${mem.max} peak since cgroup start=${mem.peak ?? 'n/a'} oom_kill this run=${kills ?? 'n/a'}`,
    )
    if (kills) {
      console.error(
        `::error::the cgroup OOM killer ran ${kills} time(s) during these checks (limit ${mem.max}): fix checkMemoryMiB, not the check`,
      )
    }
  }

  console.log(`  temp dir: ${tmpMount()}`)
  console.log(
    `  stall probe: ${stall.samples} samples, max fsync ${stall.maxFsyncMs} ms, ${stall.stalls} >= 1 s, ${stall.severeStalls} >= 5 s, max event-loop lag ${stall.maxLagMs} ms`,
  )
  for (const w of stall.worst) {
    console.log(`    ${new Date(w.at).toISOString()} fsync ${Math.round(w.fsyncMs)} ms during ${w.running.join(', ') || 'nothing'}`)
  }
  console.log(`  stalled ms (cgroup PSI, this run): ${JSON.stringify(stall.stalledMs)} memory.events delta: ${JSON.stringify(stall.events)}`)

  if (failed.length > 0) {
    for (const r of failed) console.error(`::error::${r.unit.pkg} :: ${r.unit.script} failed`)
    console.error(`::error::${failed.length} of ${results.length} checks failed`)
    // Not process.exit(): it drops what stdout still buffers, which is the failing check's output.
    process.exitCode = 1
    return
  }
  console.log(`\nall ${results.length} checks passed`)
}

await main()
