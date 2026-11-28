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
// doubles the backoff. Only the first up->down transition and failed trials count toward the backoff; failures from
// requests that were already in flight when the remote dropped are ignored, so one restart costs one 2 s backoff. Requests that arrive while the trial is in flight fail fast too.

export const REMOTE_DOWN_INITIAL_BACKOFF_MS = 2_000
export const REMOTE_DOWN_MAX_BACKOFF_MS = 30_000
// [daveey] A trial (connect, which may redial and probe, plus the request) must settle within this or it is
// treated as a failed trial: the slot is freed and the backoff escalates. Without it a trial on a slow link held the
// slot for 3.5 min (2026-11-12) while every other request failed fast with "reconnect in progress".
//
// Trade-off, chosen on purpose: this is SHORTER than a trial request's own budget (DEFAULT_FETCH_TIMEOUT_MS = 30 s,
// stretched further on a slow link, plus the body allowance for uploads). A trial whose request is still in flight at
// 20 s is rejected to its caller, but the request itself is not cancelled: the server may still receive and apply it,
// including a non-idempotent POST/PUT/DELETE, so a caller that retries can submit twice. We accept that because the
// trial is one request per backoff window and freeing the slot is what lets the remote recover; the abandoned
// request keeps running, and if it answers late (sent in the current epoch) that answer still reopens the gate.
export const REMOTE_DOWN_TRIAL_DEADLINE_MS = 20_000

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

const TRANSPORT_MESSAGE_RE =
  /\b(?:ECONNRESET|ECONNREFUSED|ECONNABORTED|EPIPE|EHOSTUNREACH|ENETUNREACH|EHOSTDOWN)\b|socket hang up/i

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
  // Failed trials so far, plus the first up->down transition. Only these escalate the backoff: requests that were
  // already in flight when the remote dropped all fail together, and counting each would jump the backoff straight
  // to the cap for a single ~10 s restart.
  failures: number
  reason: string
  since: number
  retryAt: number
  // The admission that owns the trial slot. Only that request's verdict may release it, escalate or reopen.
  trialId: null | number
}

export interface RemoteDownAdmission {
  trial: boolean
  // Per-key status epoch at admission (bumped on every up<->down transition) and, for the trial, its id. A request
  // admitted in an earlier epoch is stale: its failure says nothing new about this outage.
  readonly epoch?: number
  readonly trialId?: number
  // Epoch when the request was actually sent (after connect, which may have reconnected). An answer to a request sent
  // during the current epoch is fresh evidence, even if it was not the trial (e.g. a failed dispatch probe marked the
  // remote down and the same request's reconnect then succeeded).
  sentEpoch?: number
}

export class RemoteDownGate {
  readonly #down = new Map<string, DownState>()
  readonly #epoch = new Map<string, number>()
  readonly #now: () => number
  readonly #initialBackoffMs: number
  readonly #maxBackoffMs: number
  #nextTrialId = 1

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

  epochOf(key: string): number {
    return this.#epochOf(key)
  }

  #epochOf(key: string): number {
    return this.#epoch.get(key) ?? 0
  }

  #bumpEpoch(key: string): void {
    this.#epoch.set(key, this.#epochOf(key) + 1)
  }

  #backoff(failures: number): number {
    return Math.min(this.#initialBackoffMs * 2 ** (failures - 1), this.#maxBackoffMs)
  }

  /** Throws RemoteBackendDownError while `key` is down; otherwise admits the request (as the trial when down). */
  admit(key: string): RemoteDownAdmission {
    const state = this.#down.get(key)
    const epoch = this.#epochOf(key)

    if (!state) {
      return { trial: false, epoch }
    }

    const now = this.#now()

    if (state.trialId !== null || now < state.retryAt) {
      throw new RemoteBackendDownError(key, state.reason, now - state.since, Math.max(0, state.retryAt - now))
    }

    const trialId = this.#nextTrialId++
    state.trialId = trialId

    return { trial: true, epoch, trialId }
  }

  #ownsTrial(state: DownState, admission?: RemoteDownAdmission): boolean {
    return Boolean(admission?.trial && admission.trialId !== undefined && admission.trialId === state.trialId)
  }

  /**
   * Record that `key` failed. Without an admission (e.g. a failed dispatch probe) this is outside evidence: it takes
   * an up remote down but never escalates an existing outage. With an admission, only a current-epoch request may take
   * an up remote down, and only the owning trial may escalate the backoff and release the trial slot; stale in-flight
   * failures are ignored.
   */
  markDown(key: string, reason: string, admission?: RemoteDownAdmission): void {
    const now = this.#now()
    const state = this.#down.get(key)
    const cleanReason = String(reason || 'unreachable').slice(0, 160)

    if (!state) {
      const seenEpoch = admission?.sentEpoch ?? admission?.epoch

      if (seenEpoch !== undefined && seenEpoch !== this.#epochOf(key)) {
        return
      }

      this.#bumpEpoch(key)
      this.#down.set(key, {
        failures: 1,
        reason: cleanReason,
        since: now,
        retryAt: now + this.#backoff(1),
        trialId: null
      })

      return
    }

    if (!this.#ownsTrial(state, admission)) {
      return
    }

    state.failures += 1
    state.reason = cleanReason
    state.retryAt = now + this.#backoff(state.failures)
    state.trialId = null
  }

  /**
   * The backend answered. While down, only the owning trial (or an outside caller with no admission) reopens the
   * gate; an answer to a request admitted before the outage may predate the reset.
   */
  markUp(key: string, admission?: RemoteDownAdmission): boolean {
    const state = this.#down.get(key)

    if (!state) {
      return false
    }

    const fresh = admission?.sentEpoch !== undefined && admission.sentEpoch === this.#epochOf(key)

    if (admission && !this.#ownsTrial(state, admission) && !fresh) {
      return false
    }

    this.#down.delete(key)
    this.#bumpEpoch(key)

    return true
  }

  /** A trial that ended without a verdict (e.g. a slow endpoint) hands the trial slot back. */
  releaseTrial(key: string, admission?: RemoteDownAdmission): void {
    const state = this.#down.get(key)

    if (state && (admission ? this.#ownsTrial(state, admission) : true)) {
      state.trialId = null
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
  // Total budget for a trial (connect + request); defaults to REMOTE_DOWN_TRIAL_DEADLINE_MS.
  trialDeadlineMs?: number
}

/**
 * Run one registry request through the down gate.
 *
 * - known down → RemoteBackendDownError immediately (no dial, no timeout wait);
 * - the connection breaks (connect fails, or the request resets/refuses/hangs up) → mark down;
 * - any answer from the backend, HTTP errors included → mark up;
 * - a request timeout → forget the answer mark (next dispatch re-probes) and, if it was the trial, stay down;
 * - a trial that has not settled within its deadline → failed trial: mark down (escalates the backoff, frees the
 *   slot) and reject with a timeout. The abandoned connect/request may still settle later; it no longer owns the
 *   trial, so only a fresh answer can still reopen the gate.
 */
export async function dispatchThroughRemoteDownGate<TConnection extends { baseUrl?: null | string }, TResult>(
  options: GatedRemoteDispatchOptions<TConnection, TResult>
): Promise<TResult> {
  const { gate, key, trialDeadlineMs = REMOTE_DOWN_TRIAL_DEADLINE_MS, log } = options
  const admission = gate.admit(key)

  if (!admission.trial) {
    return runGatedDispatch(options, admission)
  }

  const state: { connection?: TConnection } = {}
  const run = runGatedDispatch(options, admission, state)
  let timer: ReturnType<typeof setTimeout> | undefined

  const deadline = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => {
      const message = `Timed out connecting to Hermes backend after ${trialDeadlineMs}ms (down-gate trial deadline)`
      options.forgetAnswer?.(state.connection?.baseUrl)
      gate.markDown(key, message, admission)
      log?.(`[remote-down] ${key} trial exceeded ${trialDeadlineMs} ms; slot freed, backoff escalated`)
      reject(new Error(message))
    }, trialDeadlineMs)
  })

  // The loser of the race must never surface as an unhandled rejection.
  run.catch(() => {})

  try {
    return await Promise.race([run, deadline])
  } finally {
    clearTimeout(timer)
  }
}

async function runGatedDispatch<TConnection extends { baseUrl?: null | string }, TResult>(
  { gate, key, connect, request, forgetAnswer, log }: GatedRemoteDispatchOptions<TConnection, TResult>,
  admission: RemoteDownAdmission,
  state: { connection?: TConnection } = {}
): Promise<TResult> {
  const { trial } = admission
  let connection: TConnection

  try {
    connection = await connect()
    state.connection = connection
  } catch (error) {
    if (isRemoteBackendDownError(error)) {
      throw error
    }

    // Config refusals ("No connection with id", bad profile, passive read with no warm backend) say nothing
    // about the remote's health.
    const message = String((error as { message?: unknown } | null)?.message ?? error)

    if (/^No connection with id|does not exist|^Passive read: no warm backend/.test(message)) {
      if (trial) {
        gate.releaseTrial(key, admission)
      }

      throw error
    }

    const wasUp = !gate.isDown(key)
    gate.markDown(key, `connect failed: ${message}`, admission)

    if (wasUp) {
      log?.(
        `[remote-down] ${key} marked down (connect failed: ${message.slice(0, 160)}); failing fast until it answers`
      )
    }

    throw error
  }

  try {
    admission.sentEpoch = gate.epochOf(key)
    const result = await request(connection)

    if (gate.markUp(key, admission)) {
      log?.(`[remote-down] ${key} answered again; gate open`)
    }

    return result
  } catch (error) {
    if (isRemoteTransportFailure(error)) {
      forgetAnswer?.(connection?.baseUrl)
      const wasUp = !gate.isDown(key)
      const message = String((error as { message?: unknown } | null)?.message ?? error)
      gate.markDown(key, message, admission)

      if (wasUp) {
        log?.(`[remote-down] ${key} marked down (${message.slice(0, 160)}); failing fast until it answers`)
      }
    } else if (isRemoteTimeout(error)) {
      forgetAnswer?.(connection?.baseUrl)

      if (trial) {
        gate.markDown(key, 'trial request timed out', admission)
      }
    } else if (gate.markUp(key, admission)) {
      // An HTTP status error (4xx/5xx) or a bad payload is still an answer: the backend is reachable.
      log?.(`[remote-down] ${key} answered again; gate open`)
    }

    throw error
  }
}
