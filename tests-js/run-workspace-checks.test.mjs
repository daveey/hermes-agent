import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, symlinkSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from 'vitest'

import {
  DEFAULT_UNIT_MIB,
  RESERVED_MIB,
  memoryBudgetMiB,
  oomKills,
  schedule,
  unitsOf,
} from '../.github/scripts/workspace-check-admission.mjs'

const GIB = 2 ** 30

/** Run `units` through the scheduler with fake checks; record what ran together. */
async function trace(units, { concurrency, budgetMiB }) {
  const live = new Set()
  let peakMiB = 0
  let peakCount = 0
  const order = []
  const results = await schedule(units, {
    concurrency,
    budgetMiB,
    run: async (unit) => {
      live.add(unit)
      order.push(unit.script)
      const used = [...live].reduce((sum, u) => sum + Math.min(u.memoryMiB, budgetMiB), 0)
      peakMiB = Math.max(peakMiB, used)
      peakCount = Math.max(peakCount, live.size)
      await new Promise((r) => setTimeout(r, 5))
      live.delete(unit)
      return unit.script
    },
  })
  return { results, peakMiB, peakCount, order }
}

const unit = (script, memoryMiB) => ({ pkg: 'apps/desktop', script, memoryMiB })

// The four apps/desktop units that started together on an 8 GiB slot and
// were OOM-killed (runs 41009740205 and 41016178444).
const desktop = [
  unit('check:test:ui', 3072),
  unit('check:test:desktop:platforms', 2048),
  unit('check:test:desktop:all', 3072),
  unit('check:lint', 3072),
]

test('an 8 GiB runner never admits more declared memory than its budget', async () => {
  const budgetMiB = memoryBudgetMiB({ constrained: 8 * GIB, total: 32 * GIB })
  expect(budgetMiB).toBe(8 * 1024 - RESERVED_MIB)
  const light = [unit('a', DEFAULT_UNIT_MIB), unit('b', DEFAULT_UNIT_MIB)]
  const { results, peakMiB, peakCount } = await trace([...desktop, ...light], { concurrency: 4, budgetMiB })
  expect(results).toHaveLength(6)
  expect(peakMiB).toBeLessThanOrEqual(budgetMiB)
  expect(peakCount).toBeGreaterThan(1)
})

test('a later unit that fits starts before an earlier one that does not', async () => {
  const { order } = await trace([unit('big1', 3000), unit('big2', 3000), unit('small', 500)], {
    concurrency: 4,
    budgetMiB: 4000,
  })
  expect(order).toEqual(['big1', 'small', 'big2'])
})

test('a unit bigger than the whole budget still runs, alone', async () => {
  const { results, peakCount } = await trace([unit('huge', 20000), unit('x', 100)], { concurrency: 4, budgetMiB: 4000 })
  expect(results.sort()).toEqual(['huge', 'x'])
  expect(peakCount).toBe(1)
})

test('CPU concurrency still caps light units', async () => {
  const many = Array.from({ length: 9 }, (_, i) => unit(`u${i}`, 10))
  const { peakCount } = await trace(many, { concurrency: 3, budgetMiB: 100000 })
  expect(peakCount).toBe(3)
})

test('the budget is the cgroup limit when set, else physical memory', () => {
  expect(memoryBudgetMiB({ constrained: 8 * GIB, total: 64 * GIB })).toBe(8192 - RESERVED_MIB)
  // No limit: Node reports 0 or 2^64-1.
  expect(memoryBudgetMiB({ constrained: 0, total: 16 * GIB })).toBe(16384 - RESERVED_MIB)
  expect(memoryBudgetMiB({ constrained: 2 ** 64 - 1, total: 16 * GIB })).toBe(16384 - RESERVED_MIB)
  expect(memoryBudgetMiB({ constrained: undefined, total: 16 * GIB })).toBe(16384 - RESERVED_MIB)
})

test('declared peaks attach to units; undeclared units get the default', () => {
  const pkgs = [
    { location: 'apps/desktop', scripts: { 'check:lint': 'x', 'check:test:ui': 'y', check: 'z' } },
    { location: 'web', scripts: { check: 'w' } },
  ]
  const units = unitsOf(pkgs, (pkg) => (pkg.location === 'apps/desktop' ? { 'check:lint': 3000 } : undefined))
  expect(units).toEqual([
    { pkg: 'apps/desktop', script: 'check:lint', memoryMiB: 3000 },
    { pkg: 'apps/desktop', script: 'check:test:ui', memoryMiB: DEFAULT_UNIT_MIB },
    { pkg: 'web', script: 'check', memoryMiB: DEFAULT_UNIT_MIB },
  ])
})

test("apps/desktop's real manifest keeps its heavy units from all starting together on an 8 GiB slot", async () => {
  const location = 'apps/desktop'
  const manifest = JSON.parse(readFileSync(new URL('../apps/desktop/package.json', import.meta.url), 'utf-8'))
  const units = unitsOf([{ location, scripts: manifest.scripts }], () => manifest.checkMemoryMiB)
  const budgetMiB = memoryBudgetMiB({ constrained: 8 * GIB, total: 32 * GIB })
  // The four units that were SIGKILLed together must not fit together.
  const killedTogether = ['check:lint', 'check:test:desktop:all', 'check:test:desktop:platforms', 'check:test:ui']
  const declared = units.filter((u) => killedTogether.includes(u.script)).reduce((sum, u) => sum + u.memoryMiB, 0)
  expect(declared).toBeGreaterThan(budgetMiB)
  const { results, peakMiB, peakCount } = await trace(units, { concurrency: 4, budgetMiB })
  expect(results).toHaveLength(units.length)
  expect(peakMiB).toBeLessThanOrEqual(budgetMiB)
  expect(peakCount).toBeLessThan(4)
})

test('the CLI runs when started through a symlinked path, so it never exits green having run nothing', () => {
  const dir = mkdtempSync(join(tmpdir(), 'rwc-'))
  try {
    const link = join(dir, 'run-workspace-checks.mjs')
    symlinkSync(fileURLToPath(new URL('../.github/scripts/run-workspace-checks.mjs', import.meta.url)), link)
    const out = execFileSync(process.execPath, [link, '--list'], {
      cwd: fileURLToPath(new URL('..', import.meta.url)),
      encoding: 'utf-8',
    })
    expect(out).toMatch(/^apps\/desktop :: check:lint {2}\(\d+ MiB\)$/m)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('oom_kill is read from memory.events, and its absence is not zero', () => {
  const events = 'low 0\nhigh 0\nmax 12\noom 3\noom_kill 2\noom_group_kill 0\n'
  expect(oomKills(events)).toBe(2)
  expect(oomKills('low 0\nmax 0\n')).toBeNull()
  expect(oomKills(null)).toBeNull()
})

test('a declared peak for a check that does not exist is an error, not a silent default', () => {
  const pkgs = [{ location: 'apps/desktop', scripts: { 'check:lint': 'x' } }]
  expect(() => unitsOf(pkgs, () => ({ 'check:typecheck': 3000 }))).toThrow(/not a check unit/)
  expect(() => unitsOf(pkgs, () => ({ 'check:lint': '3000' }))).toThrow(/positive integer/)
})
