import { isDefinitiveSessionMutationRefusal } from '../../../shared/src/session-http-mutations'

/**
 * [daveey] Ride a session write (archive, pin, unread, delete) across a backend reconnect.
 *
 * A remote backend (the hub) drops several times a day: a `hermes update` there retires its serve
 * process, the SSH tunnel resets, and Electron's remote-down gate then fails every request fast
 * ("Remote backend … is down …; reconnect in progress") until a trial request reopens it. On
 * 2026-11-12 the MacBook logged nine hub session PATCHes that died that way (`socket hang up`,
 * the fail-fast refusal, a dial timeout, `Passive read: no warm backend`), each one a user click
 * that had to be repeated once the hub was back a few seconds later.
 *
 * A fenced session write is safe to replay (identity-less legacy writes are excluded):
 * the mutation client keeps the same request_id and expected
 * revision for the same intent until the owner answers definitively, so a write the owner already
 * committed is answered from its receipt instead of applied twice. So a transport failure (the
 * renderer never reached the owner, or lost its reply) is retried here with backoff for a bounded
 * window, and only a write that still fails after it reaches the caller (archive then restores the
 * row and offers Retry). A backend verdict is never retried here: a definitive refusal (conflict,
 * not found, permission, client upgrade) ends the write, and a 503 owner_unavailable control_timeout
 * already rode out the owner's stall server-side before answering, so it goes to the user's Retry.
 *
 * The exception is a 503 saying the owner is restarting (`owner_unavailable: starting|draining`):
 * the backend waits each phase out for at most ~24 s inside the 30 s request deadline, but a whole
 * hub restart is a 30 s drain plus a ~25 s start (hub, 2026-11-24 05:23Z: an archive 10 s into the
 * drain failed with "Archive failed ... draining"). Nothing was written, so it is replayed with
 * the same identity for up to `SESSION_WRITE_OWNER_RESTART_BUDGET_MS`.
 */
export const SESSION_WRITE_RECONNECT_BUDGET_MS = 60_000
export const SESSION_WRITE_OWNER_RESTART_BUDGET_MS = 150_000
const FIRST_DELAY_MS = 1_000
const MAX_DELAY_MS = 10_000

/** A legacy write has no receipt identity: a lost reply cannot safely be replayed. */
export class NonReplayableSessionWriteError extends Error {}

const RECONNECTABLE =
  /socket hang up|\b(?:ECONNRESET|ECONNREFUSED|ECONNABORTED|EPIPE|EHOSTUNREACH|ENETUNREACH|EHOSTDOWN|ETIMEDOUT)\b|Remote backend "[^"]*" is down|Timed out connecting to Hermes backend|Passive read: no warm backend/i

const OWNER_RESTARTING = /\bowner_unavailable: (?:starting|draining): /

/** A backend answer that the owning gateway is mid-restart: nothing was written, only time is needed. */
export function isOwnerRestartingSessionWriteError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return !(error instanceof NonReplayableSessionWriteError) && OWNER_RESTARTING.test(message)
}

/** True for a failure that says nothing about the write itself: the route to its owner was down. */
export function isReconnectableSessionWriteError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return (
    !(error instanceof NonReplayableSessionWriteError) &&
    RECONNECTABLE.test(message) &&
    !isDefinitiveSessionMutationRefusal(message) &&
    !/client_upgrade_required|mutation_identity_required/.test(message)
  )
}

/** The remote-down gate's own "next try in Ns" hint, so a retry never lands inside its backoff. */
function gateRetryHintMs(error: unknown): number {
  const match = /next try in (\d+)s/.exec(error instanceof Error ? error.message : String(error))

  return match ? Number(match[1]) * 1_000 : 0
}

export interface SessionWriteReconnectOptions {
  /** Short label for the log line, e.g. `PATCH 20261112_055658_bb1bdd`. */
  label: string
  budgetMs?: number
  /** The window once the owner answered that it is restarting (starting/draining). */
  ownerRestartBudgetMs?: number
  now?: () => number
  sleep?: (ms: number) => Promise<void>
  log?: (line: string) => void
  /** The caller may hold a cancellable maintenance intent beyond a transport deadline. */
  retryable?: (error: unknown) => boolean
}

function desktopLog(line: string): void {
  try {
    console.info(line)
    window.hermesDesktop?.logLine?.(line)
  } catch {
    // logging must never break a write
  }
}

export async function withSessionWriteReconnect<T>(
  run: (remainingMs: number) => Promise<T>,
  options: SessionWriteReconnectOptions
): Promise<T> {
  const { label, now = Date.now, log = desktopLog } = options
  let budgetMs = options.budgetMs ?? SESSION_WRITE_RECONNECT_BUDGET_MS
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)))
  const started = now()
  let delay = FIRST_DELAY_MS
  let lastError: unknown = new Error('Session write reconnect deadline exceeded')

  for (let attempt = 1; ; attempt += 1) {
    const remainingMs = budgetMs - (now() - started)

    if (remainingMs <= 0) { throw lastError }

    try {
      const result = await run(remainingMs)

      if (attempt > 1) {
        log(`[session-write] ${label} landed on attempt ${attempt} after ${now() - started} ms`)
      }

      return result
    } catch (error) {
      lastError = error
      const wait = Math.max(delay, gateRetryHintMs(error))
      const reason = (error instanceof Error ? error.message : String(error)).slice(0, 160)
      const restarting = isOwnerRestartingSessionWriteError(error)

      if (restarting) {
        budgetMs = Math.max(budgetMs, options.ownerRestartBudgetMs ?? SESSION_WRITE_OWNER_RESTART_BUDGET_MS)
      }

      const replayable = options.retryable ? options.retryable(error) : restarting || isReconnectableSessionWriteError(error)

      if (!replayable || now() - started + wait > budgetMs) {
        if (attempt > 1 || replayable) {
          log(`[session-write] ${label} failed on attempt ${attempt} after ${now() - started} ms: ${reason}`)
        }

        throw error
      }

      log(
        `[session-write] ${label} attempt ${attempt} hit a ${restarting ? 'gateway restart' : 'reconnect'} (${reason}); replaying the same intent in ${wait} ms`
      )
      await sleep(wait)
      delay = Math.min(delay * 2, MAX_DELAY_MS)
    }
  }
}
