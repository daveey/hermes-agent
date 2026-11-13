// The renderer chunk check must tolerate a slow-but-working node: the unattended Desktop prebuild runs
// at macOS background QoS (taskpolicy -b), and on a contended host a check that takes 8 s idle took
// longer than its old fixed 300 s budget (daveey-mini 2026-11-13, prebuild 764f1731c5: spawnSync
// ETIMEDOUT failed a bundle vite had built cleanly). Only a stalled check may fail it.
import { execFileSync, spawnSync } from 'node:child_process'
import { chmodSync, copyFileSync, mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, test } from 'vitest'

const checker = new URL('../apps/desktop/scripts/assert-dist-built.mjs', import.meta.url).href

describe('renderer chunk check budget', () => {
  test('defaults to a budget sized for a throttled background build, not an idle box', async () => {
    const { chunkCheckTimeoutMs } = await import(checker)
    expect(chunkCheckTimeoutMs({})).toBeGreaterThanOrEqual(20 * 60_000)
    expect(chunkCheckTimeoutMs({ HERMES_CHUNK_CHECK_TIMEOUT_MS: '5000' })).toBe(5000)
    for (const bad of ['', '0', '-1', 'soon', 'NaN']) {
      expect(chunkCheckTimeoutMs({ HERMES_CHUNK_CHECK_TIMEOUT_MS: bad })).toBe(chunkCheckTimeoutMs({}))
    }
  })

  test('a non-integer budget falls back to the default instead of crashing spawnSync', async () => {
    // spawnSync's `timeout` must be an integer: 1500.5 throws ERR_OUT_OF_RANGE out of checkDistBuilt.
    const { chunkCheckTimeoutMs } = await import(checker)
    for (const bad of ['1500.5', '0.5', '1e-3']) {
      const ms = chunkCheckTimeoutMs({ HERMES_CHUNK_CHECK_TIMEOUT_MS: bad })
      expect(ms).toBe(chunkCheckTimeoutMs({}))
      expect(Number.isInteger(ms)).toBe(true)
    }
    expect(chunkCheckTimeoutMs({ HERMES_CHUNK_CHECK_TIMEOUT_MS: '1500.0' })).toBe(1500)
  })

  test('a fractional budget in the environment still lets checkDistBuilt return a verdict', async () => {
    const { checkDistBuilt } = await import(checker)
    const root = mkdtempSync(join(tmpdir(), 'renderer-check-fraction-'))
    const saved = process.env.HERMES_CHUNK_CHECK_TIMEOUT_MS
    try {
      mkdirSync(join(root, 'assets'))
      writeFileSync(join(root, 'index.html'), '<!doctype html>')
      writeFileSync(join(root, 'assets', 'index-fixture.js'), 'export const value = 1')
      process.env.HERMES_CHUNK_CHECK_TIMEOUT_MS = '1500.5'
      expect(checkDistBuilt(root)).toEqual({ ok: true })
    } finally {
      if (saved === undefined) delete process.env.HERMES_CHUNK_CHECK_TIMEOUT_MS
      else process.env.HERMES_CHUNK_CHECK_TIMEOUT_MS = saved
      rmSync(root, { recursive: true, force: true })
    }
  }, 120_000)

  test.skipIf(process.platform === 'win32')('a slow node passes within the budget; only overrunning it is a harness failure', () => {
    const root = mkdtempSync(join(tmpdir(), 'renderer-check-budget-'))
    try {
      mkdirSync(join(root, 'assets'))
      writeFileSync(join(root, 'index.html'), '<!doctype html>')
      writeFileSync(join(root, 'assets', 'index-fixture.js'), 'export const value = 1')
      // A node that takes ~1.5 s to start, like one starved of CPU and I/O.
      const slowNode = join(root, 'slow-node')
      writeFileSync(slowNode, `#!/bin/sh\nsleep 1.5\nexec ${JSON.stringify(process.execPath)} "$@"\n`)
      chmodSync(slowNode, 0o755)
      const check = (budgetMs) => JSON.parse(execFileSync(process.execPath, [
        '--input-type=module', '-e',
        `import { checkDistBuilt } from ${JSON.stringify(checker)}; console.log(JSON.stringify(checkDistBuilt(process.argv[1])))`,
        root,
      ], { env: { ...process.env, NODE: slowNode, HERMES_CHUNK_CHECK_TIMEOUT_MS: String(budgetMs) }, encoding: 'utf8' }))

      expect(check(30_000)).toEqual({ ok: true })
      const overrun = check(300)
      expect(overrun).toMatchObject({ ok: false, kind: 'harness' })
      expect(overrun.error).toContain('did not finish within 0.3 s')
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  }, 120_000)

  test.skipIf(process.platform === 'win32')('the CLI tells a timed-out check apart from a bad bundle and names the budget knob', () => {
    // main() resolves dist/ from its own location (scripts/..), so run a copy of the script in a scratch
    // desktop root. realpath: isMain compares argv[1] with import.meta.url, which node reports resolved.
    const root = realpathSync(mkdtempSync(join(tmpdir(), 'renderer-check-cli-')))
    try {
      mkdirSync(join(root, 'scripts'))
      for (const name of ['assert-dist-built.mjs', 'check-chunks-parse.mjs', 'utils.mjs']) {
        copyFileSync(fileURLToPath(new URL(`../apps/desktop/scripts/${name}`, import.meta.url)), join(root, 'scripts', name))
      }
      mkdirSync(join(root, 'dist', 'assets'), { recursive: true })
      writeFileSync(join(root, 'dist', 'index.html'), '<!doctype html>')
      writeFileSync(join(root, 'dist', 'assets', 'index-x.js'), 'export const value = 1')
      const slowNode = join(root, 'slow-node')
      writeFileSync(slowNode, `#!/bin/sh\nsleep 1.5\nexec ${JSON.stringify(process.execPath)} "$@"\n`)
      chmodSync(slowNode, 0o755)

      const run = spawnSync(process.execPath, [join(root, 'scripts', 'assert-dist-built.mjs')], {
        env: { ...process.env, NODE: slowNode, HERMES_CHUNK_CHECK_TIMEOUT_MS: '300' },
        encoding: 'utf8',
      })

      expect(run.status).toBe(1)
      expect(run.stderr).toContain('did not finish within 0.3 s')
      expect(run.stderr).toContain('failure of the CHECK')
      expect(run.stderr).toContain('throttled or contended host')
      // The hint itself, not only the error line above it, must name the knob to turn.
      const hint = run.stderr.slice(run.stderr.indexOf('failure of the CHECK'))
      expect(hint).toContain('HERMES_CHUNK_CHECK_TIMEOUT_MS')
      expect(run.stderr).not.toContain('would produce an app that launches to a blank page')
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  }, 120_000)
})
