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

export const POWER_SETTLE_MS = 15_000
export const POWER_SETTLE_SPREAD_MS = 10_000

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
  #suspended = false
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
    this.#settleUntil = this.#now() + this.#settleMs
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

/** Wire the gate to Electron's powerMonitor. `log` gets one line per wake with the held/skipped counts. */
export function attachPowerSettle(
  gate: PowerSettleGate,
  powerMonitor: PowerSettleMonitor,
  log?: (line: string) => void
) {
  powerMonitor.on('suspend', () => gate.suspend())

  powerMonitor.on('resume', () => {
    const { skipped } = gate.wake()
    log?.(`[power-settle] resume: ${skipped} background read(s) skipped while asleep; staggering background reads`)
  })
  // Lock/unlock without a suspend needs no settle window; only a suspend the 'resume' never cleared does.
  powerMonitor.on('unlock-screen', () => gate.noteActive())
}
