import { type ChildProcess, spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { createInterface } from 'node:readline'

import { afterEach, describe, expect, it } from 'vitest'

import { readProc } from './core/process-census'

interface StartedGateway {
  pid: number
  root: string
  home: string
}

const runners: ChildProcess[] = []
const gateways: StartedGateway[] = []

async function exitOf(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) {
    return
  }

  await new Promise<void>(resolve => child.once('exit', () => resolve()))
}

function runner(mode: string): { child: ChildProcess; ready: Promise<StartedGateway> } {
  const env = { ...process.env }
  // A new Playwright runner owns its own root, rather than inheriting vitest's.
  delete env.HERMES_E2E_RUN_TMP
  delete env.HERMES_E2E_CORE_ROOT

  const child = spawn(process.execPath, ['--import', 'tsx', path.join(import.meta.dirname, 'core/reaper-runner.ts'), mode, process.env.HERMES_PYTHON!], {
    env,
    stdio: ['pipe', 'pipe', 'pipe']
  })

  runners.push(child)
  let stderr = ''
  child.stderr!.on('data', chunk => { stderr += chunk })

  const ready = new Promise<StartedGateway>((resolve, reject) => {
    const lines = createInterface({ input: child.stdout! })
    lines.on('line', line => {
      if (line.startsWith('{')) {
        const value = JSON.parse(line)

        if (value.pid) {
          gateways.push(value)
        }

        resolve(value)
      }
    })
    child.once('error', reject)
    child.once('exit', code => {
      if (code !== 0) {
        reject(new Error(`runner exited ${code}: ${stderr}`))
      }
    })
  })

  return { child, ready }
}

afterEach(async () => {
  for (const child of runners.splice(0)) {
    if (child.exitCode === null && child.signalCode === null) {
      child.kill('SIGKILL')
    }

    await exitOf(child)
  }

  for (const gateway of gateways.splice(0)) {
    // RED must not itself leak a detached gateway. Re-check exact sandbox ownership.
    if (readProc(gateway.pid)?.environ.split('\0').includes(`HOME=${gateway.home}`)) {
      process.kill(gateway.pid, 'SIGKILL')
    }

    fs.rmSync(gateway.root, { recursive: true, force: true })
  }
})

describe.runIf(process.platform === 'linux')('real core gateway lifecycle', () => {
  it('reaps gateway ensure after a SIGKILLed runner when the next runner starts, but not a concurrent runner', async () => {
    const abandoned = runner('launch')
    const old = await abandoned.ready
    // Observe that ensure really detached a running gateway before killing its owner.
    expect(readProc(old.pid)?.cmdline).toContain('gateway run')
    abandoned.child.kill('SIGKILL')
    await exitOf(abandoned.child)
    expect(readProc(old.pid)?.cmdline).toContain('gateway run')

    const next = runner('launch')
    const current = await next.ready
    await expect.poll(() => readProc(old.pid)?.cmdline ?? '', { timeout: 20_000 }).toBe('')
    expect(fs.existsSync(old.root)).toBe(false)

    const concurrent = runner('reap')
    await concurrent.ready
    await exitOf(concurrent.child)
    expect(readProc(current.pid)?.cmdline).toContain('gateway run')
    expect(fs.existsSync(current.root)).toBe(true)
  }, 180_000)

  it('stops an unfinished fixture gateway on normal runner exit', async () => {
    const active = runner('launch')
    const gateway = await active.ready
    active.child.stdin!.end('exit\n')
    await exitOf(active.child)
    await expect.poll(() => readProc(gateway.pid)?.cmdline ?? '', { timeout: 20_000 }).toBe('')
    expect(fs.existsSync(gateway.root)).toBe(false)
  }, 120_000)
})
