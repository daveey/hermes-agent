// [daveey] Fail fast while a pooled remote backend is known to be down.
//
// A remote Hermes backend (hub, mini) restarts several times a day: every `hermes update` on that host retires its
// SSH-isolated serve/dashboard processes (web_server_skew_exit), and the tunnel then answers ECONNRESET / socket hang
// up until the Desktop redials. On 2026-10-28 every mac-mini "dial failed" cluster (11 of 11) and both hermes-hub
// ECONNRESET bursts lined up with a core update on that host, not with sleep, Tailscale or sshd keepalive.
//
// During that window each renderer poll (hub-sync/status, kanban/board, runners enrollment, ...) still dialed the dead
// descriptor or queued behind the redial, so the outage surfaced as a stream of 4-10 s "Timed out connecting to Hermes
// backend" errors and blocked UI work behind them. This gate records the outage per pool key
// (`conn:<id>::<profile>`) and, while it lasts, rejects requests at once with RemoteBackendDownError. After a short
// backoff ONE request is let through as the trial: it reprobes/redials; its success reopens the gate, its failure
// doubles the backoff. Requests that arrive while the trial is in flight fail fast too.

export const REMOTE_DOWN_INITIAL_BACKOFF_MS = 2_000
export const REMOTE_DOWN_MAX_BACKOFF_MS = 30_000

export const REMOTE_BACKEND_DOWN_CODE = 'REMOTE_BACKEND_DOWN'

export class RemoteBackendDownError extends Error {
  readonly code = REMOTE_BACKEND_DOWN_CODE
  readonly key: string
  readonly retryInMs: number

  constructor(key: string, reason: string, downForMs: number, retryInMs: number) {
    const retry = retryInMs > 0 ? `next try in ${Math.ceil(retryInMs / 1000)}s` : 'reconnect in progress'

    super(
      `Remote backend "${key}" is down (${reason}, ${Math.round(downForMs / 1000)}s ago); ${retry}. Failing fast instead of waiting for a timeout.`
    )
    this.name = 'RemoteBackendDownError'
    this.key = key
    this.retryInMs = retryInMs
  }
}

export function isRemoteBackendDownError(error: unknown): error is RemoteBackendDownError {
  return (error as { code?: unknown } | null)?.code === REMOTE_BACKEND_DOWN_CODE
}

const TRANSPORT_CODES = new Set([
  'ECONNABORTED',
  'ECONNREFUSED',
  'ECONNRESET',
  'EHOSTDOWN',
  'EHOSTUNREACH',
  'ENETUNREACH',
  'EPIPE'
])

const TRANSPORT_MESSAGE_RE = /\b(?:ECONNRESET|ECONNREFUSED|ECONNABORTED|EPIPE|EHOSTUNREACH|ENETUNREACH|EHOSTDOWN)\b|socket hang up/i
const TIMEOUT_MESSAGE_RE = /Timed out connecting to Hermes backend/i

/** The connection itself broke (reset, refused, hung up): the backend behind the tunnel is gone. */
export function isRemoteTransportFailure(error: unknown): boolean {
  const code = (error as { code?: unknown } | null)?.code

  if (typeof code === 'string' && TRANSPORT_CODES.has(code)) {
    return true
  }

  return TRANSPORT_MESSAGE_RE.test(String((error as { message?: unknown } | null)?.message ?? error ?? ''))
}

export function isRemoteTimeout(error: unknown): boolean {
  return TIMEOUT_MESSAGE_RE.test(String((error as { message?: unknown } | null)?.message ?? error ?? ''))
}

interface DownState {
  failures: number
  reason: string
  since: number
  retryAt: number
  trialInFlight: boolean
}

export interface RemoteDownAdmission {
  trial: boolean
}

export class RemoteDownGate {
  readonly #down = new Map<string, DownState>()
  readonly #now: () => number
  readonly #initialBackoffMs: number
  readonly #maxBackoffMs: number

  constructor(
    now: () => number = Date.now,
    initialBackoffMs = REMOTE_DOWN_INITIAL_BACKOFF_MS,
    maxBackoffMs = REMOTE_DOWN_MAX_BACKOFF_MS
  ) {
    this.#now = now
    this.#initialBackoffMs = initialBackoffMs
    this.#maxBackoffMs = maxBackoffMs
  }

  isDown(key: string): boolean {
    return this.#down.has(key)
  }

  /** Throws RemoteBackendDownError while `key` is down; otherwise admits the request (as the trial when down). */
  admit(key: string): RemoteDownAdmission {
    const state = this.#down.get(key)

    if (!state) {
      return { trial: false }
    }

    const now = this.#now()

    if (state.trialInFlight || now < state.retryAt) {
      throw new RemoteBackendDownError(key, state.reason, now - state.since, Math.max(0, state.retryAt - now))
    }

    state.trialInFlight = true

    return { trial: true }
  }

  markDown(key: string, reason: string): void {
    const now = this.#now()
    const previous = this.#down.get(key)
    const failures = (previous?.failures ?? 0) + 1
    const backoff = Math.min(this.#initialBackoffMs * 2 ** (failures - 1), this.#maxBackoffMs)

    this.#down.set(key, {
      failures,
      reason: String(reason || 'unreachable').slice(0, 160),
      since: previous?.since ?? now,
      retryAt: now + backoff,
      trialInFlight: false
    })
  }

  markUp(key: string): boolean {
    return this.#down.delete(key)
  }

  /** A trial that ended without a verdict (e.g. a slow endpoint) hands the trial slot back. */
  releaseTrial(key: string): void {
    const state = this.#down.get(key)

    if (state) {
      state.trialInFlight = false
    }
  }
}

export interface GatedRemoteDispatchOptions<TConnection extends { baseUrl?: null | string }, TResult> {
  gate: RemoteDownGate
  key: string
  connect: () => Promise<TConnection>
  request: (connection: TConnection) => Promise<TResult>
  // Drop the "answered recently" mark so the next dispatch re-probes /api/health instead of trusting a dead tunnel.
  forgetAnswer?: (baseUrl: null | string | undefined) => void
  log?: (message: string) => void
}

/**
 * Run one registry request through the down gate.
 *
 * - known down → RemoteBackendDownError immediately (no dial, no timeout wait);
 * - the connection breaks (connect fails, or the request resets/refuses/hangs up) → mark down;
 * - any answer from the backend, HTTP errors included → mark up;
 * - a request timeout → forget the answer mark (next dispatch re-probes) and, if it was the trial, stay down.
 */
export async function dispatchThroughRemoteDownGate<TConnection extends { baseUrl?: null | string }, TResult>({
  gate,
  key,
  connect,
  request,
  forgetAnswer,
  log
}: GatedRemoteDispatchOptions<TConnection, TResult>): Promise<TResult> {
  const { trial } = gate.admit(key)
  let connection: TConnection

  try {
    connection = await connect()
  } catch (error) {
    if (isRemoteBackendDownError(error)) {
      throw error
    }

    // Config refusals ("No connection with id", bad profile, passive read with no warm backend) say nothing
    // about the remote's health.
    const message = String((error as { message?: unknown } | null)?.message ?? error)

    if (/^No connection with id|does not exist|^Passive read: no warm backend/.test(message)) {
      if (trial) {
        gate.releaseTrial(key)
      }

      throw error
    }

    const wasUp = !gate.isDown(key)
    gate.markDown(key, `connect failed: ${message}`)

    if (wasUp) {
      log?.(`[remote-down] ${key} marked down (connect failed: ${message.slice(0, 160)}); failing fast until it answers`)
    }

    throw error
  }

  try {
    const result = await request(connection)

    if (gate.markUp(key)) {
      log?.(`[remote-down] ${key} answered again; gate open`)
    }

    return result
  } catch (error) {
    if (isRemoteTransportFailure(error)) {
      forgetAnswer?.(connection?.baseUrl)
      const wasUp = !gate.isDown(key)
      const message = String((error as { message?: unknown } | null)?.message ?? error)
      gate.markDown(key, message)

      if (wasUp) {
        log?.(`[remote-down] ${key} marked down (${message.slice(0, 160)}); failing fast until it answers`)
      }
    } else if (isRemoteTimeout(error)) {
      forgetAnswer?.(connection?.baseUrl)

      if (trial) {
        gate.markDown(key, 'trial request timed out')
      }
    } else if (gate.markUp(key)) {
      // An HTTP status error (4xx/5xx) or a bad payload is still an answer: the backend is reachable.
      log?.(`[remote-down] ${key} answered again; gate open`)
    }

    throw error
  }
}
