import { describe, expect, it } from 'vitest'

import {
  attachIdleMotionPause,
  IDLE_MOTION_ACTIVE_POLL_MS,
  IDLE_MOTION_PAUSE_AFTER_SECONDS,
  IDLE_MOTION_PAUSE_CSS,
  IDLE_MOTION_PAUSED_POLL_MS,
  IdleMotionPause,
  type IdleMotionTarget
} from './idle-motion-pause'

class FakeContents implements IdleMotionTarget {
  destroyed = false
  inserted = new Map<string, string>()
  removed: string[] = []
  #next = 0
  listeners: Record<string, Array<() => void>> = {}

  constructor(readonly id: number) {}

  isDestroyed() {
    return this.destroyed
  }

  insertCSS(css: string) {
    const key = `k${this.id}-${++this.#next}`
    this.inserted.set(key, css)

    return Promise.resolve(key)
  }

  removeInsertedCSS(key: string) {
    this.removed.push(key)
    this.inserted.delete(key)

    return Promise.resolve()
  }

  on(event: string, listener: () => void) {
    ;(this.listeners[event] ??= []).push(listener)

    return this
  }

  /** CSS currently applied to the live page. */
  get live() {
    return [...this.inserted.values()]
  }
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0))

function harness(windows: FakeContents[]) {
  let idle = 0
  const logs: string[] = []

  const pause = new IdleMotionPause({
    idleSeconds: () => idle,
    targets: () => windows,
    log: line => logs.push(line)
  })

  return { pause, logs, setIdle: (s: number) => (idle = s) }
}

describe('IdleMotionPause', () => {
  it('leaves animations alone while the user is active', async () => {
    const win = new FakeContents(1)
    const h = harness([win])

    h.setIdle(IDLE_MOTION_PAUSE_AFTER_SECONDS - 1)

    expect(h.pause.check()).toBe(IDLE_MOTION_ACTIVE_POLL_MS)
    await flush()
    expect(win.live).toEqual([])
    expect(h.pause.paused).toBe(false)
  })

  it('pauses every window once the system is idle, and resumes on input', async () => {
    const a = new FakeContents(1)
    const b = new FakeContents(2)
    const h = harness([a, b])

    h.setIdle(IDLE_MOTION_PAUSE_AFTER_SECONDS)
    expect(h.pause.check()).toBe(IDLE_MOTION_PAUSED_POLL_MS)
    await flush()
    expect(a.live).toEqual([IDLE_MOTION_PAUSE_CSS])
    expect(b.live).toEqual([IDLE_MOTION_PAUSE_CSS])

    // Staying idle does not stack a second copy.
    h.setIdle(IDLE_MOTION_PAUSE_AFTER_SECONDS + 60)
    h.pause.check()
    await flush()
    expect(a.live).toHaveLength(1)

    h.setIdle(0)
    expect(h.pause.check()).toBe(IDLE_MOTION_ACTIVE_POLL_MS)
    await flush()
    expect(a.live).toEqual([])
    expect(b.live).toEqual([])
    expect(h.logs).toEqual([
      `[idle-motion] no input for ${IDLE_MOTION_PAUSE_AFTER_SECONDS}s: pausing window animations`,
      '[idle-motion] input resumed: resuming window animations'
    ])
  })

  it('re-applies to a window that reloads or opens while paused', async () => {
    const a = new FakeContents(1)
    const windows = [a]
    const h = harness(windows)

    h.setIdle(IDLE_MOTION_PAUSE_AFTER_SECONDS)
    h.pause.check()
    await flush()

    // A reload drops the page's inserted stylesheets.
    a.inserted.clear()
    h.pause.noteLoaded(a)
    await flush()
    expect(a.live).toEqual([IDLE_MOTION_PAUSE_CSS])

    const b = new FakeContents(2)
    windows.push(b)
    h.pause.check()
    await flush()
    expect(b.live).toEqual([IDLE_MOTION_PAUSE_CSS])
  })

  it('skips destroyed windows and survives insertCSS failures', async () => {
    const gone = new FakeContents(1)
    gone.destroyed = true
    const broken = new FakeContents(2)
    broken.insertCSS = () => Promise.reject(new Error('render frame disposed'))
    const h = harness([gone, broken])

    h.setIdle(IDLE_MOTION_PAUSE_AFTER_SECONDS)
    h.pause.check()
    await flush()
    expect(gone.live).toEqual([])

    h.setIdle(0)
    h.pause.check()
    await flush()
    expect(broken.removed).toEqual([])
  })

  it('never pauses when the idle time cannot be read', () => {
    const win = new FakeContents(1)

    const pause = new IdleMotionPause({
      idleSeconds: () => {
        throw new Error('unsupported')
      },
      targets: () => [win]
    })

    expect(pause.check()).toBe(IDLE_MOTION_ACTIVE_POLL_MS)
    expect(pause.paused).toBe(false)
  })
})

describe('attachIdleMotionPause', () => {
  it('polls slowly while active, every second while paused, and re-applies on did-finish-load', async () => {
    const win = new FakeContents(7)
    let idle = 0
    const scheduled: Array<{ fn: () => void; ms: number }> = []
    let createdListener: ((event: unknown, contents: FakeContents) => void) | undefined

    const pause = attachIdleMotionPause({
      app: {
        on: (_event, listener) => {
          createdListener = listener as typeof createdListener
        }
      },
      powerMonitor: { getSystemIdleTime: () => idle },
      BrowserWindow: { getAllWindows: () => [{ webContents: win }] },
      timers: {
        setTimeout: (fn, ms) => {
          scheduled.push({ fn, ms })

          return {}
        }
      }
    })

    createdListener?.({}, win)
    expect(scheduled.map(s => s.ms)).toEqual([IDLE_MOTION_ACTIVE_POLL_MS])

    idle = IDLE_MOTION_PAUSE_AFTER_SECONDS + 1
    scheduled.shift()!.fn()
    expect(scheduled.map(s => s.ms)).toEqual([IDLE_MOTION_PAUSED_POLL_MS])
    await flush()
    expect(win.live).toEqual([IDLE_MOTION_PAUSE_CSS])

    win.inserted.clear()
    win.listeners['did-finish-load']!.forEach(listener => listener())
    await flush()
    expect(win.live).toEqual([IDLE_MOTION_PAUSE_CSS])

    idle = 2
    scheduled.shift()!.fn()
    expect(scheduled.map(s => s.ms)).toEqual([IDLE_MOTION_ACTIVE_POLL_MS])
    await flush()
    expect(win.live).toEqual([])
    expect(pause.paused).toBe(false)
  })
})
