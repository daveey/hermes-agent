import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  attachPowerSettle,
  isBackgroundRead,
  isPowerSettlingError,
  POWER_SETTLE_MS,
  PowerSettleGate
} from './power-settle'

function harness(opts: { random?: number } = {}) {
  let now = 1_000_000
  const sleeps: number[] = []
  const pending: Array<() => void> = []

  const gate = new PowerSettleGate({
    now: () => now,
    random: () => opts.random ?? 0,
    sleep: ms => {
      sleeps.push(ms)

      return new Promise<void>(resolve => pending.push(resolve))
    }
  })

  return {
    gate,
    sleeps,
    advance: (ms: number) => (now += ms),
    releaseAll: () => pending.splice(0).forEach(resolve => resolve())
  }
}

const poll = { method: 'GET', path: '/api/plugins/hub-sync/status', connectionId: 'hermes-hub' }

describe('isBackgroundRead', () => {
  it('holds only non-foreground GETs', () => {
    expect(isBackgroundRead({ method: 'GET' })).toBe(true)
    expect(isBackgroundRead({})).toBe(true)
    expect(isBackgroundRead({ method: 'POST' })).toBe(false)
    expect(isBackgroundRead({ method: 'GET', priority: 'foreground' })).toBe(false)
  })
})

describe('PowerSettleGate', () => {
  it('admits everything immediately while awake and outside the settle window', async () => {
    const { gate, sleeps } = harness()
    await expect(gate.admit(poll)).resolves.toBeUndefined()
    expect(sleeps).toEqual([])
  })

  it('fails background reads fast while suspended (DarkWake timers) without sleeping', async () => {
    const { gate, sleeps } = harness()
    gate.suspend()
    const error = await gate.admit(poll).catch(e => e)
    expect(isPowerSettlingError(error)).toBe(true)
    expect(String(error.message)).not.toMatch(/Timed out connecting/)
    expect(sleeps).toEqual([])
  })

  it('never holds user work while suspended: POSTs and foreground GETs pass', async () => {
    const { gate } = harness()
    gate.suspend()
    await expect(gate.admit({ ...poll, method: 'POST' })).resolves.toBeUndefined()
    await expect(gate.admit({ ...poll, priority: 'foreground' })).resolves.toBeUndefined()
  })

  it('a foreground request while still marked suspended clears the stale suspend', async () => {
    const { gate } = harness()
    gate.suspend()
    await gate.admit({ ...poll, priority: 'foreground' })
    expect(gate.suspended).toBe(false)
  })

  it('after resume, staggers background reads to the end of the settle window plus jitter', async () => {
    const { gate, sleeps, releaseAll } = harness({ random: 0.5 })
    gate.suspend()
    gate.wake()
    const admitted = gate.admit(poll)
    expect(sleeps).toEqual([POWER_SETTLE_MS + 5_000])
    releaseAll()
    await expect(admitted).resolves.toBeUndefined()
  })

  it('coalesces a re-fired poll: the same connection/path already held fails fast instead of queueing', async () => {
    const { gate, sleeps, releaseAll } = harness()
    gate.wake()
    const first = gate.admit(poll)
    const second = await gate.admit(poll).catch(e => e)
    expect(isPowerSettlingError(second)).toBe(true)
    // A different path on the same connection is its own poller and is held, not dropped.
    const other = gate.admit({ ...poll, path: '/api/plugins/inbox/items' })
    expect(sleeps).toHaveLength(2)
    releaseAll()
    await expect(first).resolves.toBeUndefined()
    await expect(other).resolves.toBeUndefined()
    // Once released the key is free again.
    expect(gate.settling()).toBe(true)
  })

  it('a held read that wakes into a new suspend does not dial', async () => {
    const { gate, releaseAll } = harness()
    gate.wake()
    const held = gate.admit(poll)
    gate.suspend()
    releaseAll()
    expect(isPowerSettlingError(await held.catch(e => e))).toBe(true)
  })

  it('stops holding once the settle window has passed', async () => {
    const { gate, sleeps, advance } = harness()
    gate.wake()
    advance(POWER_SETTLE_MS + 1)
    expect(gate.settling()).toBe(false)
    await gate.admit(poll)
    expect(sleeps).toEqual([])
  })

  it('noteActive is a no-op when awake (no settle window after a plain unlock)', () => {
    const { gate } = harness()
    gate.noteActive()
    expect(gate.settling()).toBe(false)
  })
})

describe('attachPowerSettle', () => {
  it('wires suspend/resume/unlock-screen and logs the skipped count on resume', async () => {
    const listeners = new Map<string, () => void>()
    const monitor = { on: (event: string, fn: () => void) => listeners.set(event, fn) }
    const { gate } = harness()
    const lines: string[] = []
    attachPowerSettle(gate, monitor, line => lines.push(line))

    listeners.get('suspend')!()
    expect(gate.suspended).toBe(true)
    await gate.admit(poll).catch(() => undefined)
    listeners.get('resume')!()
    expect(gate.suspended).toBe(false)
    expect(gate.settling()).toBe(true)
    expect(lines).toEqual([expect.stringContaining('resume: 1 background read(s) skipped')])
    expect(lines[0]).not.toMatch(/Timed out connecting/)

    listeners.get('suspend')!()
    listeners.get('unlock-screen')!()
    expect(gate.suspended).toBe(false)
  })
})

describe('main.ts wiring', () => {
  const main = readFileSync(join(__dirname, 'main.ts'), 'utf8')

  it('admits every hermes:api request through the power-settle gate before dispatching it', () => {
    const handler = main.slice(main.indexOf("ipcMain.handle('hermes:api'"))
    const admit = handler.indexOf('await powerSettleGate.admit(request)')
    expect(admit).toBeGreaterThan(0)
    expect(admit).toBeLessThan(handler.indexOf('handleHermesApiRequest(request)'))
    expect(admit).toBeLessThan(handler.indexOf('dispatchConnectionScopedProfileDelete('))
  })

  it('does not write a failure line (stack) for a skipped background read', () => {
    const handler = main.slice(main.indexOf("ipcMain.handle('hermes:api'"))
    const skip = handler.indexOf('if (isPowerSettlingError(error))')
    expect(skip).toBeGreaterThan(0)
    expect(skip).toBeLessThan(handler.indexOf('rememberLog(formatApiRequestFailure(request, error))'))
  })

  it('attaches the gate to powerMonitor and clears a stale suspend on window focus', () => {
    const register = main.slice(main.indexOf('function registerPowerResumeListeners()'))
    expect(register).toContain('attachPowerSettle(powerSettleGate, powerMonitor, rememberLog)')
    expect(register).toContain("app.on('browser-window-focus', () => powerSettleGate.noteActive())")
  })
})
