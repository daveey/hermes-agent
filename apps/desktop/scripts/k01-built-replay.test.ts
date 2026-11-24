import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { test } from 'vitest'

const desktop = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..')
const script = path.join(desktop, 'scripts/k01-built-replay.mjs')
const companion = path.join(desktop, 'src/contrib/fixtures/k01/session-tasks.plugin.js.txt')

test('committed built replay writes a PASS receipt and exits 0', () => {
  const source = fs.readFileSync(script, 'utf8')
  assert.equal(source.includes('trustedReadPlaceholder'), false)
  assert.match(source, /reloaded\.refresh\s*=\s*reloaded\.warm/)
  assert.doesNotMatch(source, /\bst\.refresh\s*=/)
  const out = fs.mkdtempSync(path.join(os.tmpdir(), 'k01-built-replay-'))

  try {
    const stdout = execFileSync(process.execPath, [script, companion, out], {
      encoding: 'utf8',
      cwd: desktop,
      timeout: 180_000,
      stdio: ['ignore', 'pipe', 'pipe']
    })
    const receipt = JSON.parse(fs.readFileSync(path.join(out, 'receipt.json'), 'utf8')) as { checks: string[] }
    assert.match(stdout, /"status":"PASS"/)
    assert.ok(Array.isArray(receipt.checks) && receipt.checks.length >= 8)
  } finally {
    fs.rmSync(out, { recursive: true, force: true })
  }
}, 240_000)
