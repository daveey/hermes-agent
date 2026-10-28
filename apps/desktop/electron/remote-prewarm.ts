// Hold every remote gateway warm: at startup, dial each SSH connection in connections.json (SSH ControlMaster +
// tunnel + backend reuse) for its default profile and every named profile opened in the last 24 h, then keep it up with a forced health probe every tickMs and a backoff reconnect when it
// drops. A dispatch then finds a pooled, recently-answered descriptor and never waits on an SSH dial or a spawn.
//
// Electron-free: main.ts injects the registry read, the ensure call and the clock.

export const PREWARM_TICK_MS = 15_000
export const PREWARM_BACKOFF_MS = [2_000, 5_000, 15_000, 30_000, 60_000]
// Named remote profiles are held warm while they were opened recently: each one is its own remote backend, so the
// set stays bounded to what the user actually uses.
export const RECENT_PROFILE_TTL_MS = 24 * 60 * 60_000
export const RECENT_PROFILES_PER_CONNECTION = 4

export interface PrewarmTarget {
  connectionId: string
  profile: string
}

export function prewarmTargetKey(target: PrewarmTarget): string {
  return `${target.connectionId}::${target.profile}`
}

type RecentProfileMap = Record<string, Record<string, number>>

/** Named remote profiles the user dispatched to recently, per SSH connection; persisted so a cold start warms them. */
export class RecentRemoteProfiles {
  readonly #load: () => unknown
  readonly #save: (data: RecentProfileMap) => void
  readonly #now: () => number
  #data: RecentProfileMap | null = null

  constructor(opts: { load: () => unknown; save: (data: RecentProfileMap) => void; now?: () => number }) {
    this.#load = opts.load
    this.#save = opts.save
    this.#now = opts.now ?? Date.now
  }

  #read(): RecentProfileMap {
    if (!this.#data) {
      let raw: unknown

      try {
        raw = this.#load()
      } catch {
        raw = null
      }

      this.#data = {}

      if (raw && typeof raw === 'object') {
        for (const [id, profiles] of Object.entries(raw as Record<string, unknown>)) {
          if (profiles && typeof profiles === 'object') {
            this.#data[id] = Object.fromEntries(
              Object.entries(profiles as Record<string, unknown>).filter(([, at]) => typeof at === 'number')
            ) as Record<string, number>
          }
        }
      }
    }

    return this.#data
  }

  // The default profile is always warm; only named profiles are recorded. Writes at most once a minute per profile.
  touch(connectionId: string, profile: string): void {
    if (!connectionId || !profile || profile === 'default') {
      return
    }

    const data = this.#read()
    const now = this.#now()
    const profiles = (data[connectionId] ??= {})

    if (now - (profiles[profile] ?? 0) < 60_000) {
      return
    }

    profiles[profile] = now

    const kept = Object.entries(profiles)
      .filter(([, at]) => now - at < RECENT_PROFILE_TTL_MS)
      .sort((a, b) => b[1] - a[1])
      .slice(0, RECENT_PROFILES_PER_CONNECTION)

    data[connectionId] = Object.fromEntries(kept)

    try {
      this.#save(data)
    } catch {
      void 0
    }
  }

  profilesFor(connectionId: string): string[] {
    const now = this.#now()

    return Object.entries(this.#read()[connectionId] ?? {})
      .filter(([, at]) => now - at < RECENT_PROFILE_TTL_MS)
      .sort((a, b) => b[1] - a[1])
      .slice(0, RECENT_PROFILES_PER_CONNECTION)
      .map(([profile]) => profile)
  }
}

// Every SSH connection's default profile plus its recently used named profiles.
export function prewarmTargets(connectionIds: string[], recent: RecentRemoteProfiles): PrewarmTarget[] {
  return connectionIds.flatMap(connectionId => [
    { connectionId, profile: 'default' },
    ...recent.profilesFor(connectionId).map(profile => ({ connectionId, profile }))
  ])
}

export interface RemotePrewarmDeps {
  // Re-read every tick so added/removed connections and newly used profiles converge.
  listTargets: () => PrewarmTarget[]
  // Dial or revalidate one target; `forceProbe` bypasses the dispatch probe skip so a dropped tunnel is noticed.
  ensure: (target: PrewarmTarget, opts: { forceProbe: boolean }) => Promise<unknown>
  log: (line: string) => void
  now?: () => number
  setTimer?: (fn: () => void, ms: number) => { unref?: () => void }
  clearTimer?: (timer: unknown) => void
  tickMs?: number
}

interface ConnectionState {
  failures: number
  nextAttemptAt: number
  warm: boolean
}

export class RemotePrewarmer {
  readonly #deps: RemotePrewarmDeps
  readonly #states = new Map<string, ConnectionState>()
  readonly #inflight = new Set<string>()
  #timer: unknown = null
  #stopped = true

  constructor(deps: RemotePrewarmDeps) {
    this.#deps = deps
  }

  get running(): boolean {
    return !this.#stopped
  }

  // `key` is a connection id (its default profile) or a prewarmTargetKey().
  isWarm(key: string): boolean {
    return this.#states.get(key.includes('::') ? key : `${key}::default`)?.warm === true
  }

  // Dial every connection now (in parallel) and start the keepalive loop. Resolves when the first round settles.
  async start(): Promise<void> {
    if (!this.#stopped) {
      return
    }

    this.#stopped = false
    await this.tick(true)
    this.#schedule()
  }

  stop(): void {
    this.#stopped = true

    if (this.#timer) {
      ;(this.#deps.clearTimer ?? clearTimeout)(this.#timer as any)
      this.#timer = null
    }
  }

  // One round. The first round dials (no probe to force); later rounds force the probe on warm connections.
  async tick(initial = false): Promise<void> {
    const now = (this.#deps.now ?? Date.now)()
    const targets = this.#deps.listTargets()
    const keys = targets.map(prewarmTargetKey)

    for (const known of [...this.#states.keys()]) {
      if (!keys.includes(known)) {
        this.#states.delete(known)
      }
    }

    await Promise.all(
      targets.map(async target => {
        const id = prewarmTargetKey(target)
        const state = this.#states.get(id) ?? { failures: 0, nextAttemptAt: 0, warm: false }
        this.#states.set(id, state)

        if (this.#inflight.has(id) || now < state.nextAttemptAt || this.#stopped) {
          return
        }

        this.#inflight.add(id)
        const startedAt = (this.#deps.now ?? Date.now)()

        try {
          await this.#deps.ensure(target, { forceProbe: !initial && state.warm })

          if (!state.warm || state.failures) {
            this.#deps.log(
              `[remote-prewarm] ${id} ${state.failures ? 'reconnected' : 'warm'} in ${(this.#deps.now ?? Date.now)() - startedAt} ms`
            )
          }

          state.warm = true
          state.failures = 0
          state.nextAttemptAt = 0
        } catch (error: any) {
          const delay = PREWARM_BACKOFF_MS[Math.min(state.failures, PREWARM_BACKOFF_MS.length - 1)]
          state.warm = false
          state.failures += 1
          state.nextAttemptAt = (this.#deps.now ?? Date.now)() + delay
          this.#deps.log(
            `[remote-prewarm] ${id} unavailable (${error?.message || error}); retry ${state.failures} in ${delay} ms`
          )
        } finally {
          this.#inflight.delete(id)
        }
      })
    )
  }

  #schedule(): void {
    if (this.#stopped) {
      return
    }

    const setTimer = this.#deps.setTimer ?? ((fn: () => void, ms: number) => setTimeout(fn, ms))

    const timer = setTimer(() => {
      void this.tick().finally(() => this.#schedule())
    }, this.#deps.tickMs ?? PREWARM_TICK_MS)

    ;(timer as { unref?: () => void } | undefined)?.unref?.()
    this.#timer = timer
  }
}
