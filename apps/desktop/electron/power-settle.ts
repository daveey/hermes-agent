// [daveey] Hold background reads while the Mac sleeps and for a short settle window after it wakes.
//
// On 2026-10-29 desktop.log showed "Timed out connecting to Hermes backend" in bursts at every sleep/wake
// (17:04 x14, 17:07 x18, 17:48 x46): macOS DarkWake runs the app's timers for a few seconds with the network and
// CPU half up, and on the full wake every renderer poller (hub-sync/status, user-steps, inbox, session-mentions,
// session-kanban-tasks, ...) fires at the same instant against a CPU-starved local backend and SSH tunnels that are
// still being rebuilt (attachPowerResumeRemoteRevalidation). Each of those reads then waits its full 4-15 s timeout.
//
// This gate sits in front of the `hermes:api` IPC handler:
// - between powerMonitor 'suspend' and the next wake signal (DarkWake never emits 'resume'), a background GET fails
//   at once with PowerSettlingError instead of dialing;
// - for SETTLE_MS after the wake, a background GET waits until the window ends plus a random 0..SPREAD_MS, so the
//   pollers come back staggered after the tunnels are rebuilt instead of all at once during the rebuild. A poller
//   that fires again while its previous read is still held fails fast, so a 2 s poll does not queue a dozen copies.
// User-driven work is never held: anything that is not a GET, or that carries priority 'foreground', passes through.
// A user action (window focus, a foreground request) while still marked suspended counts as a wake, so a missed
// 'resume' cannot hold background reads forever.
//
// 2026-10-30: macOS only sent 'resume' at the FIRST DarkWake after a clamshell sleep (17:08); the next 4.5 h of
// Maintenance Sleep / DarkWake cycles and the final DarkWake -> FullWake (21:31:30) emitted neither 'suspend' nor
// 'resume', so the gate stayed open: reads in flight when the Mac froze expired at the next wake (21:31:26
// session-mentions/outbox, "after 10000ms") and the pollers ran into tunnels still rebuilding (21:31:38-48
// "Remote backend ... is down"). So the gate also
// - treats a wall-clock gap (no tick for CLOCK_GAP_MS while a 5 s ticker runs) as a missed sleep -> wake;
// - absorbs a background read's connect failure (timeout / remote down / refused) when the Mac slept while the read
//   was in flight (it times out the moment the process thaws), or within RECONNECT_GRACE_MS after a wake while
//   tunnels reconnect. It fails as PowerSettlingError (the next poll retries) and is counted in one summary line
//   when the grace ends.

export const POWER_SETTLE_MS = 15_000
export const POWER_SETTLE_SPREAD_MS = 10_000
export const POWER_CLOCK_TICK_MS = 5_000
export const POWER_CLOCK_GAP_MS = 30_000
export const POWER_RECONNECT_GRACE_MS = 60_000

// Connect-level failures a sleep or a tunnel rebuild produces. A server answer (HTTP status) never matches.
const CONNECT_FAILURE_RE =
  /Timed out connecting to Hermes backend|Remote backend .* is down|ECONNREFUSED|ECONNRESET|EPIPE|ETIMEDOUT|socket hang up|SSH operation .* timed out/

export const POWER_SETTLING_CODE = 'POWER_SETTLING'

export class PowerSettlingError extends Error {
  readonly code = POWER_SETTLING_CODE

  constructor() {
    super('Background read skipped: the machine is asleep or settling after a wake; the next poll retries.')
    this.name = 'PowerSettlingError'
  }
}

export function isPowerSettlingError(error: unknown): error is PowerSettlingError {
  return (error as { code?: unknown } | null)?.code === POWER_SETTLING_CODE
}

export interface PowerSettleRequest {
  connectionId?: unknown
  method?: unknown
  path?: unknown
  priority?: unknown
  profile?: unknown
}

function heldKey(request: PowerSettleRequest): string {
  return [request.connectionId, request.profile, request.path].map(v => String(v ?? '')).join('|')
}

export interface PowerSettleOptions {
  now?: () => number
  random?: () => number
  sleep?: (ms: number) => Promise<void>
  settleMs?: number
  spreadMs?: number
  clockGapMs?: number
  reconnectGraceMs?: number
}

/** True for a failure at the connect layer (timeout, refused, remote down), not a server response. */
export function isConnectFailure(error: unknown): boolean {
  return CONNECT_FAILURE_RE.test(String((error as { message?: unknown } | null)?.message ?? error ?? ''))
}

/** True for a request the gate may hold: a background (non-foreground) GET. */
export function isBackgroundRead(request: PowerSettleRequest | null | undefined): boolean {
  const method = String(request?.method ?? 'GET').toUpperCase()

  return method === 'GET' && request?.priority !== 'foreground'
}

export class PowerSettleGate {
  readonly #now: () => number
  readonly #random: () => number
  readonly #sleep: (ms: number) => Promise<void>
  readonly #settleMs: number
  readonly #spreadMs: number
  readonly #clockGapMs: number
  readonly #reconnectGraceMs: number
  #suspended = false
  #lastTick: number
  #lastWakeAt = -Infinity
  #absorbed = 0
  #log?: (line: string) => void
  #settleUntil = 0
  #held = 0
  #skipped = 0
  // Background reads waiting out the settle window, by connection|profile|path. A poller that fires again while its
  // previous read is still held gets PowerSettlingError instead of queueing a second copy.
  readonly #waiting = new Set<string>()

  constructor(options: PowerSettleOptions = {}) {
    this.#now = options.now ?? Date.now
    this.#random = options.random ?? Math.random
    this.#sleep = options.sleep ?? (ms => new Promise(resolve => setTimeout(resolve, ms)))
    this.#settleMs = options.settleMs ?? POWER_SETTLE_MS
    this.#spreadMs = options.spreadMs ?? POWER_SETTLE_SPREAD_MS
    this.#clockGapMs = options.clockGapMs ?? POWER_CLOCK_GAP_MS
    this.#reconnectGraceMs = options.reconnectGraceMs ?? POWER_RECONNECT_GRACE_MS
    this.#lastTick = this.#now()
  }

  setLog(log: ((line: string) => void) | undefined): void {
    this.#log = log
  }

  /**
   * Called every POWER_CLOCK_TICK_MS by attachPowerSettle, and before every decision. A gap longer than clockGapMs
   * means the process was frozen (the Mac slept) without a powerMonitor event: start a settle window as on 'resume'.
   * Also writes the reconnect-grace summary once the grace after a wake has passed.
   */
  observeClock(): void {
    const now = this.#now()
    const gap = now - this.#lastTick
    this.#lastTick = now

    if (gap > this.#clockGapMs) {
      const { skipped } = this.wake()
      this.#log?.(
        `[power-settle] clock-gap wake: no tick for ${Math.round(gap / 1000)}s (sleep without a resume event); ` +
          `${skipped} skipped; staggering background reads`
      )
    }

    if (this.#absorbed > 0 && now - this.#lastWakeAt >= this.#reconnectGraceMs) {
      this.#log?.(`[power-settle] ${this.#absorbed} background read(s) failed to connect across a sleep/wake; absorbed`)
      this.#absorbed = 0
    }
  }

  /**
   * A background read failed. True if it is a connect-level failure while suspended or within the reconnect grace
   * after a wake (including a wake this call itself detects from a clock gap: a read in flight when the Mac froze
   * times out the moment it thaws). The caller then throws PowerSettlingError instead of logging a failure; the
   * next poll retries. False for a real failure that must be logged.
   */
  absorbFailure(request: PowerSettleRequest | null | undefined, error: unknown): boolean {
    if (!isBackgroundRead(request) || !isConnectFailure(error)) {
      return false
    }

    this.observeClock()

    if (!this.#suspended && this.#now() - this.#lastWakeAt >= this.#reconnectGraceMs) {
      return false
    }

    this.#absorbed += 1

    return true
  }

  get suspended(): boolean {
    return this.#suspended
  }

  /** powerMonitor 'suspend': hold background reads until a wake signal. */
  suspend(): void {
    this.#suspended = true
  }

  /** powerMonitor 'resume' (or a stale suspend cleared by user activity): start the settle window. Returns and resets
   * the held/skipped counts. */
  wake(): { held: number; skipped: number } {
    this.#suspended = false
    this.#lastWakeAt = this.#now()
    this.#lastTick = this.#lastWakeAt
    this.#settleUntil = this.#lastWakeAt + this.#settleMs
    const counts = { held: this.#held, skipped: this.#skipped }
    this.#held = 0
    this.#skipped = 0

    return counts
  }

  /** Background reads held (staggered) since the last wake; diagnostics only. */
  get heldSinceWake(): number {
    return this.#held
  }

  /** The user is evidently active (focus, a foreground request): a still-set suspend flag is stale. */
  noteActive(): void {
    if (this.#suspended) {
      this.wake()
    }
  }

  settling(): boolean {
    return this.#suspended || this.#now() < this.#settleUntil
  }

  /**
   * Admit one `hermes:api` request. Resolves when it may be sent; throws PowerSettlingError for a background read
   * while suspended. During the settle window a background read waits until the window ends plus a random spread.
   */
  async admit(request: PowerSettleRequest | null | undefined): Promise<void> {
    this.observeClock()

    if (!isBackgroundRead(request)) {
      if (request?.priority === 'foreground') {
        this.noteActive()
      }

      return
    }

    if (this.#suspended) {
      this.#skipped += 1

      throw new PowerSettlingError()
    }

    const remaining = this.#settleUntil - this.#now()

    if (remaining <= 0) {
      return
    }

    const key = heldKey(request ?? {})

    if (this.#waiting.has(key)) {
      this.#skipped += 1

      throw new PowerSettlingError()
    }

    this.#held += 1
    this.#waiting.add(key)

    try {
      await this.#sleep(remaining + Math.floor(this.#random() * this.#spreadMs))
    } finally {
      this.#waiting.delete(key)
    }

    if (this.#suspended) {
      // Went back to sleep while waiting (DarkWake -> Sleep): don't dial into a sleeping network.
      throw new PowerSettlingError()
    }
  }
}

export interface PowerSettleMonitor {
  on(event: string, listener: () => void): unknown
}

export interface PowerSettleTimers {
  setInterval(fn: () => void, ms: number): { unref?: () => unknown }
}

/** Wire the gate to Electron's powerMonitor. `log` gets one line per wake with the held/skipped counts. */
export function attachPowerSettle(
  gate: PowerSettleGate,
  powerMonitor: PowerSettleMonitor,
  log?: (line: string) => void,
  timers: PowerSettleTimers = globalThis
) {
  gate.setLog(log)
  // A wall-clock gap between ticks is a sleep powerMonitor did not report (DarkWake/Maintenance Sleep cycles).
  timers.setInterval(() => gate.observeClock(), POWER_CLOCK_TICK_MS).unref?.()
  powerMonitor.on('suspend', () => gate.suspend())

  powerMonitor.on('resume', () => {
    const { skipped } = gate.wake()
    log?.(`[power-settle] resume: ${skipped} background read(s) skipped while asleep; staggering background reads`)
  })
  // Lock/unlock without a suspend needs no settle window; only a suspend the 'resume' never cleared does.
  powerMonitor.on('unlock-screen', () => gate.noteActive())
}
