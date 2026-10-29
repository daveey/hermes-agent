import * as fs from 'node:fs'
import * as path from 'node:path'

import { describe, expect, it } from 'vitest'

/**
 * The core lane must finish (and print its failures) inside the job's 30 min
 * limit even when every spec fails. CI 40785530488: 9 specs x 180 s blank-window
 * timeouts, then the job limit cancelled the run before Playwright printed a
 * single error, and the artifact upload hit the account quota, so nothing
 * explained the failure.
 */
const core = import.meta.dirname
const config = fs.readFileSync(path.join(core, 'playwright.config.ts'), 'utf8')
const harness = fs.readFileSync(path.join(core, 'harness.ts'), 'utf8')

const workflow = fs.readFileSync(
  path.join(core, '..', '..', '..', '..', '.github', 'workflows', 'e2e-desktop-core.yml'),
  'utf8'
)

const num = (s: string) => Number(s.replace(/_/g, ''))

describe('core lane time budget', () => {
  const testTimeout = num(/\n {2}timeout: ([\d_]+),/.exec(config)![1])
  const maxFailures = num(/\n {2}maxFailures: (\d+),/.exec(config)?.[1] ?? 'NaN')
  const jobMinutes = num(/Desktop core E2E \(Linux\)[\s\S]*?timeout-minutes: (\d+)/.exec(workflow)![1])

  it('stops a lane-wide failure well before the job limit', () => {
    expect(Number.isFinite(maxFailures)).toBe(true)
    // Failing specs alone must leave at least half the job for build + report.
    expect(maxFailures * testTimeout).toBeLessThanOrEqual((jobMinutes * 60_000) / 2)
  })

  it('waitForInteractive gives up before the test timeout, so its diagnosis is printed', () => {
    const fallback = num(/export async function waitForInteractive\([^)]*timeout = ([\d_]+)\)/.exec(harness)![1])

    expect(fallback).toBeLessThan(testTimeout)
    // and leaves room for the evaluate + log tail that builds the message
    expect(testTimeout - fallback).toBeGreaterThanOrEqual(30_000)
  })
})
