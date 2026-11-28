import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  attachPowerSettle,
  isBackgroundRead,
  isConnectFailure,
  isPowerSettlingError,
  POWER_CLOCK_GAP_MS,
  POWER_CLOCK_TICK_MS,
  POWER_RECONNECT_GRACE_MS,
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

const timeout = new Error('Timed out connecting to Hermes backend after 10000ms')

// Stay awake for `ms`: advance in regular ticks, as the 5 s ticker does, so no clock gap is seen.
function tickFor(h: ReturnType<typeof harness>, ms: number) {
  for (let left = ms; left > 0; left -= POWER_CLOCK_TICK_MS) {
    h.advance(Math.min(POWER_CLOCK_TICK_MS, left))
    h.gate.observeClock()
  }
}

describe('clock-gap wake (sleep without a powerMonitor event)', () => {
  it('a tick gap longer than the threshold opens a settle window and logs one line', async () => {
    const { gate, sleeps, advance } = harness()
    const lines: string[] = []
    gate.setLog(line => lines.push(line))
    advance(POWER_CLOCK_GAP_MS + 1_000)
    gate.observeClock()
    expect(gate.settling()).toBe(true)
    expect(lines).toEqual([expect.stringContaining('[power-settle] clock-gap wake: no tick for 31s')])
    expect(lines[0]).not.toMatch(/Timed out connecting/)
    void gate.admit(poll)
    expect(sleeps).toHaveLength(1)
  })

  it('admit itself notices the gap (first read after the freeze is held, not dialed)', async () => {
    const { gate, sleeps, advance } = harness()
    advance(POWER_CLOCK_GAP_MS * 10)
    void gate.admit(poll)
    expect(sleeps).toHaveLength(1)
  })

  it('regular ticks are not a wake', () => {
    const h = harness()
    tickFor(h, 20 * POWER_CLOCK_TICK_MS)
    expect(h.gate.settling()).toBe(false)
  })
})

describe('absorbFailure', () => {
  it('classifies connect-level failures only', () => {
    expect(isConnectFailure(timeout)).toBe(true)
    expect(
      isConnectFailure(new Error('Remote backend "conn:hermes-hub::default" is down (dispatch probe failed)'))
    ).toBe(true)
    expect(isConnectFailure(new Error('connect ECONNREFUSED 127.0.0.1:58232'))).toBe(true)
    expect(isConnectFailure(new Error('500: internal error'))).toBe(false)
  })

  it('absorbs a background read that was in flight when the Mac froze (timer expired at the thaw)', () => {
    const { gate, advance } = harness()
    advance(POWER_CLOCK_GAP_MS + 10_000) // frozen; the request timeout fires on unfreeze, before any tick
    expect(gate.absorbFailure(poll, timeout)).toBe(true)
    expect(gate.settling()).toBe(true)
  })

  it('absorbs a read failing while suspended', () => {
    const { gate } = harness()
    gate.suspend()
    expect(gate.absorbFailure(poll, timeout)).toBe(true)
  })

  it('absorbs remote-down failures during the reconnect grace after a wake, and logs one summary after it', () => {
    const h = harness()
    const { gate } = h
    const lines: string[] = []
    gate.setLog(line => lines.push(line))
    gate.wake()
    tickFor(h, 20_000)
    const down = new Error(
      'Remote backend "conn:hermes-hub::default" is down (dispatch probe failed: Timed out connecting)'
    )
    expect(gate.absorbFailure(poll, down)).toBe(true)
    expect(lines).toEqual([])
    tickFor(h, POWER_RECONNECT_GRACE_MS)
    expect(lines).toEqual(['[power-settle] 1 background read(s) failed to connect across a sleep/wake; absorbed'])
  })

  it('does not absorb when awake past the grace, for foreground or writes, or for server errors', () => {
    const h = harness()
    const { gate } = h
    gate.wake()
    tickFor(h, POWER_RECONNECT_GRACE_MS + 1)
    expect(gate.absorbFailure(poll, timeout)).toBe(false)
    gate.wake()
    expect(gate.absorbFailure({ ...poll, priority: 'foreground' }, timeout)).toBe(false)
    expect(gate.absorbFailure({ ...poll, method: 'POST' }, timeout)).toBe(false)
    expect(gate.absorbFailure(poll, new Error('500: boom'))).toBe(false)
  })
})

describe('attachPowerSettle', () => {
  it("starts an unref'd clock ticker that drives observeClock", () => {
    const monitor = { on: () => undefined }
    const { gate, advance } = harness()
    const ticks: Array<{ fn: () => void; ms: number }> = []
    let unrefd = false
    attachPowerSettle(gate, monitor, undefined, {
      setInterval: (fn, ms) => {
        ticks.push({ fn, ms })

        return { unref: () => (unrefd = true) }
      }
    })
    expect(ticks.map(t => t.ms)).toEqual([POWER_CLOCK_TICK_MS])
    expect(unrefd).toBe(true)
    advance(POWER_CLOCK_GAP_MS + 1)
    ticks[0].fn()
    expect(gate.settling()).toBe(true)
  })

  it('wires suspend/resume/unlock-screen and logs the skipped count on resume', async () => {
    const listeners = new Map<string, () => void>()
    const monitor = { on: (event: string, fn: () => void) => listeners.set(event, fn) }
    const { gate } = harness()
    const lines: string[] = []
    attachPowerSettle(gate, monitor, line => lines.push(line), { setInterval: () => ({}) })

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
    const skip = handler.indexOf('if (isPowerSettlingError(error) ||')
    expect(skip).toBeGreaterThan(0)
    expect(skip).toBeLessThan(handler.indexOf('rememberLog(formatApiRequestFailure(request, error))'))
  })

  it('absorbs a sleep-crossing connect failure before writing the failure line', () => {
    const handler = main.slice(main.indexOf("ipcMain.handle('hermes:api'"))
    const absorb = handler.indexOf('powerSettleGate.absorbFailure(request, error)')
    expect(absorb).toBeGreaterThan(0)
    expect(absorb).toBeLessThan(handler.indexOf('rememberLog(formatApiRequestFailure(request, error))'))
  })

  it('attaches the gate to powerMonitor and clears a stale suspend on window focus', () => {
    const register = main.slice(main.indexOf('function registerPowerResumeListeners()'))
    expect(register).toContain('attachPowerSettle(powerSettleGate, powerMonitor, rememberLog)')
    expect(register).toContain("app.on('browser-window-focus', () => powerSettleGate.noteActive())")
  })
})
