// [daveey] Keep a remote backend usable over a slow link instead of reconnecting it in a loop.
//
// On 2026-11-12 the MacBook sat on a ~60-175 KB/s network. The SSH tunnel to the hub was alive, but answers came
// late: the dispatch /api/health probe (10 s) timed out, the pooled backend was retired and its SSH master torn
// down, and the reconnect refetched everything (sessions, boards, transcripts) over the same link, which made the
// next probe time out too. desktop.log showed "failed its dispatch probe ... reconnecting on demand" every 1-2
// minutes for an hour while a trivial command through the tunnel took 34 s. Nothing was down; it was slow.
//
// This module holds the slow-link state per remote backend (by its forwarded baseUrl):
// - it enters slow mode ONLY when a dispatch probe times out while the transport (SSH master) is still alive
//   (noteTimeoutWhileAlive). Answer times alone never enter it: a slow endpoint on a fast link is not a slow link;
// - while slow, request and probe timeouts are stretched (scaleTimeout), the known background polls are thinned to
//   one in flight per scope and path with a minimum gap (admitBackground), and bulk work can pause (isSlow);
// - it leaves slow mode when the median of the last SLOW_LINK_EXIT_SAMPLES answers is fast, or after
//   SLOW_LINK_IDLE_EXPIRY_MS with no slow answer or timeout.
// A backend that never answers is still given up on: SLOW_LINK_STRIKE_LIMIT timeouts with no answer in between
// report giveUp, and the caller retires it as before. A dead transport is never treated as slow.
//
// Known costs:
// - `ssh -O check` proves the master process is alive, not that the link carries traffic. When a network drops
//   without a reset, the master lives until its keepalive gives up (~45 s), so for that window requests wait their
//   stretched timeout instead of failing after the 10 s probe.
// - A remote process that hangs while its tunnel stays alive looks exactly like a slow link. It is retired only after
//   SLOW_LINK_STRIKE_LIMIT (3) probe timeouts with no answer in between: from the second strike each probe waits its
//   stretched 40 s, and between probes requests wait up to SLOW_LINK_TIMEOUT_CAP_MS (60 s) each. Such a hang now
//   takes minutes to retire instead of ~10 s.

import { deltaBodyStore } from './delta-reads'
import { isBackgroundRead, type PowerSettleRequest, PowerSettlingError } from './power-settle'
import { isRemoteTimeout } from './remote-down-gate'
import { remoteAnswerLedger } from './remote-liveness'

export const SLOW_LINK_FAST_MS = 1_000
export const SLOW_LINK_EXIT_SAMPLES = 5
export const SLOW_LINK_IDLE_EXPIRY_MS = 10 * 60_000
export const SLOW_LINK_TIMEOUT_FACTOR = 4
export const SLOW_LINK_TIMEOUT_CAP_MS = 60_000
export const SLOW_LINK_STRIKE_LIMIT = 3
export const SLOW_LINK_BACKGROUND_GAP_MS = 30_000

// The renderer's untagged GETs include user-driven reads (opening a session), so thinning is limited to the paths
// that are polled on a timer: plugin status/board/outbox reads and the session-kanban strip. A skipped read of one
// of these leaves the view on the copy the previous poll delivered.
const THINNED_POLL_PREFIXES = ['/api/plugins/', '/api/session-kanban-tasks']

export function isThinnedPollPath(path: string): boolean {
  return THINNED_POLL_PREFIXES.some(prefix => path.startsWith(prefix))
}

/**
 * A background poll held back because its backend is on a slow link. It is a PowerSettlingError on purpose: the
 * renderer already treats that as "skipped, the next poll retries" (src/lib/power-settling-rejections.ts) and the
 * hermes:api handler rethrows it without a stack line, which is exactly the contract a thinned poll needs.
 */
export class SlowLinkHeldError extends PowerSettlingError {
  constructor() {
    super()
    this.message = 'Background read skipped: the connection to this backend is slow; the next poll retries.'
  }
}

interface LinkState {
  // When the link last looked slow (entered, a timeout, or an answer slower than SLOW_LINK_FAST_MS).
  evidenceAt: number
  samples: number[]
  strikes: number
}

interface BackgroundSlot {
  inFlight: boolean
  startedAt: number
}

export interface SlowLinkOptions {
  log?: (line: string) => void
  now?: () => number
}

const normalize = (baseUrl: null | string | undefined) => String(baseUrl ?? '').replace(/\/+$/, '')

const median = (values: readonly number[]) => {
  const sorted = [...values].sort((a, b) => a - b)

  return sorted[Math.floor((sorted.length - 1) / 2)] ?? 0
}

export class SlowLinkTracker {
  readonly #links = new Map<string, LinkState>()
  readonly #scopes = new Map<string, string>()
  readonly #background = new Map<string, BackgroundSlot>()
  readonly #now: () => number
  #log: ((line: string) => void) | undefined

  constructor(options: SlowLinkOptions = {}) {
    this.#now = options.now ?? Date.now
    this.#log = options.log
  }

  setLog(log: ((line: string) => void) | undefined): void {
    this.#log = log
  }

  /** Remember which forwarded baseUrl a pool scope (`conn:<id>::<profile>`) is served from. */
  bind(scope: string, baseUrl: null | string | undefined): void {
    const url = normalize(baseUrl)

    if (scope && url) {
      this.#scopes.set(scope, url)
    }
  }

  isSlow(baseUrl: null | string | undefined): boolean {
    const url = normalize(baseUrl)
    const state = this.#links.get(url)

    if (!state) {
      return false
    }

    if (this.#now() - state.evidenceAt >= SLOW_LINK_IDLE_EXPIRY_MS) {
      this.#leave(url, 'no slow answer for a while')

      return false
    }

    return true
  }

  isScopeSlow(scope: string): boolean {
    return this.isSlow(this.#scopes.get(scope))
  }

  /** Any profile of one registry connection is on a slow link (bulk work pauses per connection). */
  isConnectionSlow(connectionId: string): boolean {
    const prefix = `conn:${connectionId}::`

    for (const [scope, baseUrl] of this.#scopes) {
      if (scope.startsWith(prefix) && this.isSlow(baseUrl)) {
        return true
      }
    }

    return false
  }

  /** The timeout to use for a request to this backend: stretched while its link is slow. */
  scaleTimeout(baseUrl: null | string | undefined, timeoutMs: number): number {
    if (!this.isSlow(baseUrl) || !Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      return timeoutMs
    }

    return Math.max(timeoutMs, Math.min(timeoutMs * SLOW_LINK_TIMEOUT_FACTOR, SLOW_LINK_TIMEOUT_CAP_MS))
  }

  /** The backend answered a request successfully after elapsedMs. Only a backend in slow mode keeps samples. */
  noteAnswer(baseUrl: null | string | undefined, elapsedMs: number): void {
    const url = normalize(baseUrl)
    const state = this.#links.get(url)

    if (!state || !Number.isFinite(elapsedMs) || elapsedMs < 0) {
      return
    }

    state.strikes = 0
    state.samples.push(elapsedMs)

    if (state.samples.length > SLOW_LINK_EXIT_SAMPLES) {
      state.samples.shift()
    }

    if (elapsedMs >= SLOW_LINK_FAST_MS) {
      state.evidenceAt = this.#now()
    }

    if (state.samples.length >= SLOW_LINK_EXIT_SAMPLES && median(state.samples) < SLOW_LINK_FAST_MS) {
      this.#leave(url, 'answers are fast again')
    }
  }

  /**
   * A dispatch probe timed out while the transport (SSH master) was still alive: slow, not down. giveUp is true
   * once SLOW_LINK_STRIKE_LIMIT of these arrived with no answer in between; the caller then retires the backend.
   */
  noteTimeoutWhileAlive(baseUrl: null | string | undefined): { giveUp: boolean; strikes: number } {
    const url = normalize(baseUrl)

    if (!url) {
      return { giveUp: true, strikes: 0 }
    }

    let state = this.#links.get(url)

    if (!state) {
      state = { evidenceAt: this.#now(), samples: [], strikes: 0 }
      this.#links.set(url, state)
      this.#log?.(
        `[slow-link] ${url} is slow (probe timed out with the SSH connection alive); keeping the connection, stretching timeouts x${SLOW_LINK_TIMEOUT_FACTOR} and thinning background polls`
      )
    }

    state.strikes += 1
    state.evidenceAt = this.#now()
    state.samples.length = 0

    if (state.strikes >= SLOW_LINK_STRIKE_LIMIT) {
      const strikes = state.strikes
      this.forget(url)
      this.#log?.(`[slow-link] ${url} timed out ${strikes} times with no answer; treating it as down`)

      return { giveUp: true, strikes }
    }

    return { giveUp: false, strikes: state.strikes }
  }

  /**
   * Thin the timer-driven polls (isThinnedPollPath) to a slow backend: per scope and path, one in flight and at most
   * one start per SLOW_LINK_BACKGROUND_GAP_MS. Throws SlowLinkHeldError for a poll to skip; otherwise returns the
   * release to call when the request settles, with whether it was actually sent (an unsent one frees its slot at
   * once, so a refused dial does not cost the path its next 30 s). A fast link admits everything.
   */
  admitBackground(scope: string, path: string): (sent: boolean) => void {
    if (!isThinnedPollPath(path) || !this.isScopeSlow(scope)) {
      return () => undefined
    }

    const key = `${scope}\u0000${path}`
    const now = this.#now()
    const slot = this.#background.get(key)

    if (slot && (slot.inFlight || now - slot.startedAt < SLOW_LINK_BACKGROUND_GAP_MS)) {
      throw new SlowLinkHeldError()
    }

    // Paths carry session ids and queries: drop the slots whose gap has passed so a long slow spell stays bounded.
    for (const [other, old] of this.#background) {
      if (!old.inFlight && now - old.startedAt >= SLOW_LINK_BACKGROUND_GAP_MS) {
        this.#background.delete(other)
      }
    }

    const mine: BackgroundSlot = { inFlight: true, startedAt: now }
    this.#background.set(key, mine)

    return sent => {
      mine.inFlight = false

      if (!sent && this.#background.get(key) === mine) {
        this.#background.delete(key)
      }
    }
  }

  /** The backend was retired or its tunnel torn down: a rebuilt one starts with a clean record. */
  forget(baseUrl: null | string | undefined): void {
    const url = normalize(baseUrl)

    this.#links.delete(url)
    this.#dropBackground(url)
  }

  forgetScope(scope: string): void {
    remoteTransports.forgetScope(scope)
    this.forget(this.#scopes.get(scope))
    this.#scopes.delete(scope)
    deltaBodyStore.unbindScope(scope) // the tunnel is going away: its port must stop being asked for deltas
  }

  #leave(url: string, reason: string): void {
    this.#links.delete(url)
    this.#dropBackground(url)
    this.#log?.(`[slow-link] ${url}: ${reason}; normal timeouts and polling restored`)
  }

  #dropBackground(url: string): void {
    for (const [scope, bound] of this.#scopes) {
      if (bound !== url) {
        continue
      }

      const prefix = `${scope}\u0000`

      for (const key of this.#background.keys()) {
        if (key.startsWith(prefix)) {
          this.#background.delete(key)
        }
      }
    }
  }
}

export const slowLinkTracker = new SlowLinkTracker()

// --- main.ts wiring. It lives here because main.ts is over its line cap and may only shrink. ---

/**
 * The `slowLink` option of ensureHealthyPooledRemoteBackendForDispatch for one SSH pool scope. `log` is wired here,
 * before any probe can time out, so the "[slow-link] ... is slow" entry line is written even when no remote request
 * has answered yet in this process (noteRemoteAnswer wires it too, but only once something answers).
 */
export function dispatchSlowLink(
  scope: string,
  masterAlive: () => Promise<boolean> | undefined,
  log?: (line: string) => void,
  tracker: SlowLinkTracker = slowLinkTracker
) {
  if (log) {
    tracker.setLog(log)
  }

  return {
    isTimeout: isRemoteTimeout,
    noteTimeout: (connection: { baseUrl?: null | string }) => {
      tracker.bind(scope, connection.baseUrl)

      return tracker.noteTimeoutWhileAlive(connection.baseUrl)
    },
    transportAlive: async () => (await masterAlive()) === true
  }
}

/**
 * One registry request as the poll thinner sees it. Building it throws SlowLinkHeldError for a timer poll to skip.
 * `sent` is called when the request goes on the wire (it also ties the scope to its baseUrl); `run` wraps the
 * dispatch and frees the slot afterwards, at once if nothing was sent (down gate, failed dial).
 */
export function thinnedPoll(
  scope: null | string,
  request: (PowerSettleRequest & { path?: unknown }) | null | undefined,
  tracker: SlowLinkTracker = slowLinkTracker
) {
  const release =
    scope && isBackgroundRead(request) ? tracker.admitBackground(scope, String(request?.path || '')) : null

  let wasSent = false

  return {
    async run<T>(dispatch: () => Promise<T>): Promise<T> {
      try {
        return await dispatch()
      } finally {
        release?.(wasSent)
      }
    },
    sent(baseUrl: null | string | undefined): void {
      wasSent = true

      if (scope) {
        tracker.bind(scope, baseUrl)
      }
    }
  }
}

// Remote connection timing (part B, instant session loads): connect-start, reuse/spawn and the first request a
// new descriptor serves, per scope, as `[remote-timing]` lines in desktop.log.
const remoteFirstRequestPending = new Map<string, { scope: string; startedAt: number }>()

// masterAlive is required: without it the chat WebSocket of this tunnel can never count a strike while the SSH master
// lives, so a slow link would never stretch its connect limit.
export function noteRemoteConnect(
  baseUrl: string,
  scope: string,
  startedAt: number,
  masterAlive: () => Promise<boolean> | undefined
): void {
  remoteFirstRequestPending.set(baseUrl, { scope, startedAt })
  slowLinkTracker.bind(scope, baseUrl)
  remoteTransports.remember(baseUrl, scope, masterAlive)
  deltaBodyStore.bindRemote(baseUrl, scope) // delta-reads.ts: this tunnel's reads may come back as deltas
}

/** A backend answered `path` after elapsedMs: the answer ledger, the slow-link tracker and the first-request line. */
export function noteRemoteAnswer(baseUrl: string, path: string, elapsedMs: number, log: (line: string) => void): void {
  remoteAnswerLedger.recordAnswer(baseUrl)
  slowLinkTracker.setLog(log)
  slowLinkTracker.noteAnswer(baseUrl, elapsedMs)
  const pending = remoteFirstRequestPending.get(baseUrl)

  if (pending) {
    remoteFirstRequestPending.delete(baseUrl)
    log(`[remote-timing] ${pending.scope} first-request ${path} served +${Date.now() - pending.startedAt} ms`)
  }
}

// --- Chat WebSocket connect limit (the renderer's JsonRpcGatewayClient dial) ---
//
// The chat socket gave up after a fixed 15 s while slow-link mode kept the HTTP side of the same tunnel alive, so a
// slow link showed "Reconnecting to Hermes" in chat. The renderer asks main for the limit right before each dial
// (hermes:gateway:connect-limit, sync) and reports how the dial ended (hermes:gateway:connect-outcome):
// - a dial to an SSH-forwarded backend gets 15 s, stretched x4 (capped 60 s) while that link is in slow mode;
// - a socket that opened counts as an answer for the tracker (as a request answer does);
// - a dial that hit its limit while the SSH master is alive counts as a strike, the same way a dispatch probe does.
//   When those strikes reach SLOW_LINK_STRIKE_LIMIT the link leaves slow mode (logged as down); retiring the backend
//   stays with the dispatch probe, which runs again before the renderer's next dial (hermes:gateway:ws-url-for).
// Every other backend (local, cloud, a URL main does not know) keeps the 15 s default.

export const GATEWAY_CONNECT_LIMIT_MS = 15_000
export const GATEWAY_CONNECT_LIMIT_CHANNEL = 'hermes:gateway:connect-limit'
export const GATEWAY_CONNECT_OUTCOME_CHANNEL = 'hermes:gateway:connect-outcome'

export type GatewayConnectOutcome = { elapsedMs: number; kind: 'opened' } | { kind: 'timed-out' }

interface RemoteTransport {
  baseUrl: string
  masterAlive: () => Promise<boolean> | undefined
  scope: string
}

const originOf = (url: string, wsToHttp = false): null | string => {
  try {
    const parsed = new URL(url)
    const protocol = wsToHttp ? parsed.protocol.replace(/^ws(s?):$/, 'http$1:') : parsed.protocol

    return `${protocol}//${parsed.host}`
  } catch {
    return null
  }
}

/** SSH-forwarded backends by origin, so a chat ws:// URL can be traced to its tunnel and its slow-link state. */
export class RemoteTransports {
  readonly #byOrigin = new Map<string, RemoteTransport>()

  remember(baseUrl: string, scope: string, masterAlive: () => Promise<boolean> | undefined): void {
    const origin = originOf(baseUrl)

    if (origin) {
      this.#byOrigin.set(origin, { baseUrl, masterAlive, scope })
    }
  }

  forgetScope(scope: string): void {
    for (const [origin, transport] of this.#byOrigin) {
      if (transport.scope === scope) {
        this.#byOrigin.delete(origin)
      }
    }
  }

  forWsUrl(wsUrl: unknown): RemoteTransport | undefined {
    const origin = typeof wsUrl === 'string' ? originOf(wsUrl, true) : null

    return origin ? this.#byOrigin.get(origin) : undefined
  }
}

export const remoteTransports = new RemoteTransports()

/** The connect limit for one chat WebSocket dial: stretched only for an SSH-forwarded backend in slow mode. */
export function gatewayConnectLimitMs(
  wsUrl: unknown,
  tracker: SlowLinkTracker = slowLinkTracker,
  transports: RemoteTransports = remoteTransports
): number {
  const transport = transports.forWsUrl(wsUrl)

  return transport ? tracker.scaleTimeout(transport.baseUrl, GATEWAY_CONNECT_LIMIT_MS) : GATEWAY_CONNECT_LIMIT_MS
}

/** How a chat WebSocket dial ended: an open is an answer, a timeout with the SSH master alive is a strike. */
export async function noteGatewayConnectOutcome(
  wsUrl: unknown,
  outcome: GatewayConnectOutcome | null | undefined,
  log: (line: string) => void,
  tracker: SlowLinkTracker = slowLinkTracker,
  transports: RemoteTransports = remoteTransports
): Promise<void> {
  const transport = transports.forWsUrl(wsUrl)

  if (!transport || !outcome) {
    return
  }

  if (outcome.kind === 'opened') {
    if (Number.isFinite(outcome.elapsedMs) && outcome.elapsedMs >= 0) {
      remoteAnswerLedger.recordAnswer(transport.baseUrl)
      tracker.noteAnswer(transport.baseUrl, outcome.elapsedMs)
    }

    return
  }

  if (outcome.kind !== 'timed-out') {
    return
  }

  let alive = false

  try {
    alive = (await transport.masterAlive()) === true
  } catch {
    alive = false
  }

  if (!alive) {
    return // a dead transport is never slow; the dispatch probe retires it
  }

  tracker.setLog(log)
  tracker.bind(transport.scope, transport.baseUrl)
  const { giveUp, strikes } = tracker.noteTimeoutWhileAlive(transport.baseUrl)

  log(
    `[slow-link] ${transport.scope} chat WebSocket did not open within its limit with the SSH connection alive ` +
      `(strike ${strikes}${giveUp ? ', giving up on slow mode' : ''})`
  )
}

interface IpcMainLike {
  on(channel: string, listener: (event: { returnValue?: unknown }, ...args: any[]) => void): unknown
}

/** main.ts calls this once: the renderer's chat dial reads its limit and reports its outcome over these channels. */
export function registerGatewayConnectIpc(ipcMain: IpcMainLike, log: (line: string) => void): void {
  ipcMain.on(GATEWAY_CONNECT_LIMIT_CHANNEL, (event, wsUrl) => {
    event.returnValue = gatewayConnectLimitMs(wsUrl)
  })
  ipcMain.on(GATEWAY_CONNECT_OUTCOME_CHANNEL, (_event, wsUrl, outcome) => {
    void noteGatewayConnectOutcome(wsUrl, outcome, log)
  })
}
