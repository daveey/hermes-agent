// [daveey] Pause the windows' CSS animations while nobody is at the machine.
//
// 2026-11-13 03:36-03:47 PST on daveey-mini (David away ~85 min, HID idle 5000 s, display never sleeps): an idle,
// unfocused Desktop window cost WindowServer 44.7% + GPU helper 9.6% + renderer 12.9% of a core, on a host whose
// CPU governor was then holding work for being >90% busy. The renderer's JS was 96% idle in a 15 s V8 profile; the
// only running animation in the window was one 22 px `codicon-modifier-spin` spinner (the core-update status-bar chip,
// shown for the whole ~40 min "Building update…" phase). An infinite CSS animation asks Chromium for a new frame every
// display refresh, so the renderer compositor, the GPU process and WindowServer all recomposite at 60 Hz for a spinner
// no one is watching. Busy-session spinners do the same overnight.
//
// While the system has seen no keyboard/mouse input for `pauseAfterSeconds`, this injects one stylesheet into every
// BrowserWindow's top-level webContents that sets `animation-play-state: paused` on everything; a paused animation
// needs no frames, so the window stops repainting. Any input (polled every second while paused) removes it again.
// Windows that load or reload while paused get it re-applied. Transitions and JS-driven updates (streamed text) are
// untouched.
//
// Limits:
// - Only BrowserWindow top-level webContents are covered. `<webview>` guests (the preview pane) are separate
//   webContents that getAllWindows() does not list, so they keep animating.
// - An exit animation that starts while idle with no input (a timer-driven toast that unmounts on `animationend`)
//   is paused mid-way and stays on screen, frozen, until the next input resumes it.

export const IDLE_MOTION_PAUSE_AFTER_SECONDS = 300
export const IDLE_MOTION_ACTIVE_POLL_MS = 30_000
export const IDLE_MOTION_PAUSED_POLL_MS = 1_000

export const IDLE_MOTION_PAUSE_CSS = '*, *::before, *::after { animation-play-state: paused !important; }'

/** The slice of Electron's WebContents this needs. */
export interface IdleMotionTarget {
  readonly id: number
  isDestroyed(): boolean
  insertCSS(css: string): Promise<string>
  removeInsertedCSS(key: string): Promise<void>
}

export interface IdleMotionPauseOptions {
  /** Seconds since the last system-wide keyboard/mouse input (powerMonitor.getSystemIdleTime). */
  idleSeconds: () => number
  /** Every window's webContents right now. */
  targets: () => IdleMotionTarget[]
  pauseAfterSeconds?: number
  log?: (line: string) => void
}

export class IdleMotionPause {
  readonly #idleSeconds: () => number
  readonly #targets: () => IdleMotionTarget[]
  readonly #pauseAfterSeconds: number
  readonly #log?: (line: string) => void
  #paused = false
  // webContents id -> its inserted-CSS key (a promise until insertCSS settles; null when it failed).
  readonly #keys = new Map<number, { target: IdleMotionTarget; key: Promise<string | null> }>()

  constructor(options: IdleMotionPauseOptions) {
    this.#idleSeconds = options.idleSeconds
    this.#targets = options.targets
    this.#pauseAfterSeconds = options.pauseAfterSeconds ?? IDLE_MOTION_PAUSE_AFTER_SECONDS
    this.#log = options.log
  }

  get paused(): boolean {
    return this.#paused
  }

  /** Read the system idle time, pause or resume accordingly, and return the delay until the next check. */
  check(): number {
    let idle = 0

    try {
      idle = Number(this.#idleSeconds()) || 0
    } catch {
      idle = 0 // unknown idle time: never pause on a guess
    }

    if (idle >= this.#pauseAfterSeconds) {
      if (!this.#paused) {
        this.#paused = true
        this.#log?.(`[idle-motion] no input for ${Math.round(idle)}s: pausing window animations`)
      }

      this.#applyMissing()

      return IDLE_MOTION_PAUSED_POLL_MS
    }

    if (this.#paused) {
      this.#paused = false
      this.#log?.('[idle-motion] input resumed: resuming window animations')
      this.#removeAll()
    }

    return IDLE_MOTION_ACTIVE_POLL_MS
  }

  /** A window (re)loaded: re-apply while paused. */
  noteLoaded(target: IdleMotionTarget): void {
    // The old sheet is usually gone with the old page, but the 1 s paused tick can insert into a new window after its
    // document commits and before did-finish-load; that sheet survives the load. Remove the old key (a no-op when the
    // page already dropped it) before re-applying, or resume removes only the second copy and the window stays frozen.
    const previous = this.#keys.get(target.id)
    this.#keys.delete(target.id)

    if (previous) {
      this.#remove(previous)
    }

    if (this.#paused) {
      this.#applyMissing()
    }
  }

  #applyMissing(): void {
    for (const target of this.#targets()) {
      if (target.isDestroyed() || this.#keys.has(target.id)) {
        continue
      }

      let key: Promise<string | null>

      try {
        key = Promise.resolve(target.insertCSS(IDLE_MOTION_PAUSE_CSS)).catch(() => null)
      } catch {
        key = Promise.resolve(null)
      }

      this.#keys.set(target.id, { target, key })
    }
  }

  #removeAll(): void {
    const entries = [...this.#keys.values()]
    this.#keys.clear()

    for (const entry of entries) {
      this.#remove(entry)
    }
  }

  #remove({ target, key }: { target: IdleMotionTarget; key: Promise<string | null> }): void {
    void key.then(value => {
      if (value === null || target.isDestroyed()) {
        return
      }

      try {
        return Promise.resolve(target.removeInsertedCSS(value)).catch(() => undefined)
      } catch {
        return undefined
      }
    })
  }
}

export interface IdleMotionPauseHost {
  app: { on(event: 'web-contents-created', listener: (event: unknown, contents: IdleMotionLoadable) => void): unknown }
  powerMonitor: { getSystemIdleTime(): number }
  BrowserWindow: { getAllWindows(): Array<{ webContents: IdleMotionTarget }> }
  log?: (line: string) => void
  timers?: { setTimeout(fn: () => void, ms: number): { unref?: () => unknown } }
}

export interface IdleMotionLoadable extends IdleMotionTarget {
  on(event: 'did-finish-load', listener: () => void): unknown
}

/** Wire the pause to Electron: poll the system idle time and re-apply after window loads. */
export function attachIdleMotionPause(host: IdleMotionPauseHost): IdleMotionPause {
  const timers: NonNullable<IdleMotionPauseHost['timers']> = host.timers ?? {
    setTimeout: (fn, ms) => setTimeout(fn, ms) as unknown as { unref?: () => unknown }
  }

  const pause = new IdleMotionPause({
    idleSeconds: () => host.powerMonitor.getSystemIdleTime(),
    targets: () => host.BrowserWindow.getAllWindows().map(win => win.webContents),
    log: host.log
  })

  host.app.on('web-contents-created', (_event, contents) => {
    contents.on('did-finish-load', () => pause.noteLoaded(contents))
  })

  const tick = () => {
    const delay = pause.check()
    timers.setTimeout(tick, delay).unref?.()
  }

  timers.setTimeout(tick, IDLE_MOTION_ACTIVE_POLL_MS).unref?.()

  return pause
}
