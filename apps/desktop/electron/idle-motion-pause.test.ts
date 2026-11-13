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

  /** A reload into a new renderer process: its sheets are gone and Blink's per-process key counter starts over. */
  restartRenderer() {
    this.inserted.clear()
    this.#next = 0
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

/** `removeInsertedCSS` that throws synchronously instead of rejecting (a webContents torn down mid-call). */
class ThrowingRemoveContents extends FakeContents {
  throwOnRemove = false

  override removeInsertedCSS(key: string) {
    if (this.throwOnRemove) {
      throw new Error('removeInsertedCSS threw synchronously')
    }

    return super.removeInsertedCSS(key)
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

  it('a window whose sheet survives did-finish-load while paused is unfrozen on resume', async () => {
    // The 1 s paused tick can insert into a new window after its document commits but before did-finish-load,
    // so that sheet is still live when noteLoaded re-applies.
    const win = new FakeContents(1)
    const h = harness([win])

    h.setIdle(IDLE_MOTION_PAUSE_AFTER_SECONDS)
    h.pause.check()
    await flush()
    h.pause.noteLoaded(win)
    await flush()
    expect(win.live).toEqual([IDLE_MOTION_PAUSE_CSS])

    h.setIdle(0)
    h.pause.check()
    await flush()
    expect(win.live).toEqual([])
  })

  it('a reload while paused whose insertCSS is still pending ends with one sheet that resume removes', async () => {
    const win = new FakeContents(1)
    let release: (() => void) | undefined
    const realInsert = win.insertCSS.bind(win)
    win.insertCSS = css => new Promise(resolve => (release = () => resolve(realInsert(css))))
    const h = harness([win])

    h.setIdle(IDLE_MOTION_PAUSE_AFTER_SECONDS)
    h.pause.check()
    const firstRelease = release!
    win.insertCSS = realInsert
    h.pause.noteLoaded(win) // did-finish-load before the first insertCSS settled
    firstRelease()
    await flush()
    expect(win.live).toEqual([IDLE_MOTION_PAUSE_CSS])

    h.setIdle(0)
    h.pause.check()
    await flush()
    expect(win.live).toEqual([])
  })

  it('a reload into a new renderer process while paused keeps the fresh sheet when its key repeats the stale one', async () => {
    // insertCSS keys come from a per-renderer-process counter, so after a crash reload the new sheet's key can equal
    // the old one; removing the stale key after re-inserting would delete the fresh sheet.
    const win = new FakeContents(1)
    const h = harness([win])

    h.setIdle(IDLE_MOTION_PAUSE_AFTER_SECONDS)
    h.pause.check()
    await flush()
    expect([...win.inserted.keys()]).toEqual(['k1-1'])

    win.restartRenderer()
    h.pause.noteLoaded(win)
    await flush()
    expect([...win.inserted.keys()]).toEqual(['k1-1'])
    expect(win.live).toEqual([IDLE_MOTION_PAUSE_CSS])

    // The 1 s paused tick does not stack a second copy while the re-apply is in flight or after it.
    h.pause.check()
    await flush()
    expect(win.live).toEqual([IDLE_MOTION_PAUSE_CSS])

    h.setIdle(0)
    h.pause.check()
    await flush()
    expect(win.live).toEqual([])
  })

  it('a resume while a reload re-apply waits on the stale removal still leaves the window unfrozen', async () => {
    const win = new FakeContents(1)
    const h = harness([win])

    h.setIdle(IDLE_MOTION_PAUSE_AFTER_SECONDS)
    h.pause.check()
    await flush()

    win.restartRenderer()
    h.pause.noteLoaded(win)
    h.setIdle(0)
    h.pause.check() // input arrives before the stale removal and the re-insert settle
    await flush()
    expect(win.live).toEqual([])
  })

  // [daveey] #2323 follow-up (t_01a1d595575df431): a stale-key removal that throws synchronously is swallowed like a
  // rejection, so the reload's re-apply still runs instead of rejecting the chain and leaving the page unpaused.
  it('re-applies after a reload whose stale-sheet removal throws synchronously', async () => {
    const win = new ThrowingRemoveContents(1)
    const h = harness([win])

    h.setIdle(IDLE_MOTION_PAUSE_AFTER_SECONDS)
    h.pause.check()
    await flush()

    win.restartRenderer()
    win.throwOnRemove = true
    h.pause.noteLoaded(win)
    await flush()
    await flush()
    expect(win.live).toEqual([IDLE_MOTION_PAUSE_CSS])

    win.throwOnRemove = false
    h.setIdle(0)
    h.pause.check()
    await flush()
    expect(win.live).toEqual([])
  })

  // [daveey] #2337 follow-up (t_01a1d595575df431, from t_01a1d59498376333): a window that loads while the user is
  // active gets no sheet, even after an earlier pause.
  it('a window that loads after resume stays unpaused', async () => {
    const win = new FakeContents(1)
    const h = harness([win])

    h.setIdle(IDLE_MOTION_PAUSE_AFTER_SECONDS)
    h.pause.check()
    await flush()
    h.setIdle(0)
    h.pause.check()
    await flush()

    h.pause.noteLoaded(win)
    await flush()
    await flush()
    expect(win.live).toEqual([])
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
