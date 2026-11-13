// The renderer chunk check must tolerate a slow-but-working node: the unattended Desktop prebuild runs
// at macOS background QoS (taskpolicy -b), and on a contended host a check that takes 8 s idle took
// longer than its old fixed 300 s budget (daveey-mini 2026-11-13, prebuild 764f1731c5: spawnSync
// ETIMEDOUT failed a bundle vite had built cleanly). Only a stalled check may fail it.
import { execFileSync } from 'node:child_process'
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
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
})
