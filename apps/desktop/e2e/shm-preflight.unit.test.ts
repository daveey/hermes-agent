import * as fs from 'node:fs'
import * as path from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import { assertShmLargeEnough, MIN_SHM_BYTES, realShmBytes, shmProblem } from './shm-preflight'

const MiB = 1024 * 1024

describe('shmProblem', () => {
  it("names Docker's 64 MiB default as too small on Linux", () => {
    // The gen-2 CI box (CI 40785530488) ran runner containers without
    // --shm-size; Electron's window stayed blank and every spec timed out.
    const problem = shmProblem({ platform: 'linux', shmBytes: () => 64 * MiB })

    expect(problem).toContain('64 MiB')
    expect(problem).toContain('--shm-size')
  })

  it('accepts the 2g the CI boxes give their containers', () => {
    expect(shmProblem({ platform: 'linux', shmBytes: () => 2048 * MiB })).toBeNull()
  })

  it('accepts exactly the minimum', () => {
    expect(shmProblem({ platform: 'linux', shmBytes: () => MIN_SHM_BYTES })).toBeNull()
  })

  it('does not block a host where /dev/shm cannot be measured', () => {
    expect(shmProblem({ platform: 'linux', shmBytes: () => null })).toBeNull()
  })

  it('is Linux-only: macOS and Windows have no /dev/shm limit to check', () => {
    const probe = () => {
      throw new Error('must not probe off Linux')
    }

    expect(shmProblem({ platform: 'darwin', shmBytes: probe })).toBeNull()
    expect(shmProblem({ platform: 'win32', shmBytes: probe })).toBeNull()
  })
})

describe('assertShmLargeEnough', () => {
  const saved = process.env.HERMES_E2E_ALLOW_SMALL_SHM

  afterEach(() => {
    if (saved === undefined) {
      delete process.env.HERMES_E2E_ALLOW_SMALL_SHM
    } else {
      process.env.HERMES_E2E_ALLOW_SMALL_SHM = saved
    }
  })

  it('throws on a small /dev/shm', () => {
    delete process.env.HERMES_E2E_ALLOW_SMALL_SHM
    expect(() => assertShmLargeEnough({ platform: 'linux', shmBytes: () => 64 * MiB })).toThrow(/\/dev\/shm has 64 MiB/)
  })

  it('can be overridden explicitly', () => {
    process.env.HERMES_E2E_ALLOW_SMALL_SHM = '1'
    expect(() => assertShmLargeEnough({ platform: 'linux', shmBytes: () => 64 * MiB })).not.toThrow()
  })
})

describe('realShmBytes', () => {
  it.runIf(process.platform === 'linux' && fs.existsSync('/dev/shm'))('measures the real /dev/shm size', () => {
    const bytes = realShmBytes()

    expect(typeof bytes).toBe('number')
    expect(bytes).toBeGreaterThan(0)
  })
})

describe('the Electron lanes run the preflight', () => {
  // Both entry paths: the required core lane and the update lane each have
  // their own Playwright config and global setup.
  const e2e = import.meta.dirname

  it('core: playwright config wires global-setup.ts, which asserts /dev/shm', () => {
    const config = fs.readFileSync(path.join(e2e, 'core', 'playwright.config.ts'), 'utf8')
    const setup = fs.readFileSync(path.join(e2e, 'core', 'global-setup.ts'), 'utf8')

    expect(config).toMatch(/globalSetup: '\.\/global-setup\.ts'/)
    expect(setup).toMatch(/assertShmLargeEnough\(\)/)
  })

  it('update: global setup asserts /dev/shm before building the install', () => {
    const setup = fs.readFileSync(path.join(e2e, 'update', 'global-setup.ts'), 'utf8')
    const body = setup.slice(setup.indexOf('export default function globalSetup'))

    expect(body.indexOf('assertShmLargeEnough()')).toBeGreaterThan(-1)
    expect(body.indexOf('assertShmLargeEnough()')).toBeLessThan(body.indexOf('seedInstall'))
  })
})
