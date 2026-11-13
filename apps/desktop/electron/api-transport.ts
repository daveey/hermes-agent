/**
 * Shared HTTP transport policy for the Electron main process's Hermes REST
 * helpers (fetchJson / fetchPublicJson / downloadViaTokenToFile).
 *
 * Two concerns live here so they can be unit-tested without Electron:
 *
 * 1. Connection-pooled keep-alive agents. Opening a fresh TCP socket per call
 *    is what produced the burst-traffic ECONNRESET storms (#92976): the
 *    backend closes idle keep-alive sockets and the next write on a reused
 *    raw socket dies with 'socket hang up'. JSON calls and streaming
 *    downloads get SEPARATE pools so a handful of long-lived download
 *    streams can never starve the small, latency-sensitive JSON calls out of
 *    the socket pool.
 *
 * 2. A retry policy that is safe for non-idempotent verbs. A transient
 *    transport error does NOT mean the server didn't process the request —
 *    an ECONNRESET can arrive after the backend already handled a POST
 *    (created the session, submitted the prompt) and merely lost the socket
 *    before the response was read. Blindly retrying every verb double-submits.
 *
 *    The rule implemented by shouldRetryRequest():
 *      - Idempotent verbs (GET / HEAD / OPTIONS) retry on any transient
 *        transport error — replaying them is harmless by definition.
 *      - Non-idempotent verbs (POST / PUT / PATCH / DELETE) retry ONLY when
 *        the request provably never reached the server:
 *          a) connection-establishment failures (ECONNREFUSED, ENOTFOUND,
 *             EAI_AGAIN, EHOSTUNREACH, ENETUNREACH) — no connection means no
 *             request; or
 *          b) a transient error thrown before we started flushing the
 *             request (requestState.bodySent === false).
 *        Anything ambiguous — ECONNRESET / EPIPE / 'socket hang up' after
 *        the body went out — is NOT retried; the error surfaces to the
 *        caller. When in doubt, don't retry a non-idempotent request.
 */

import crypto from 'node:crypto'
import http from 'node:http'
import https from 'node:https'

// JSON pool: many small concurrent calls (session lists, config, prompts).
const HTTP_JSON_AGENT = new http.Agent({ keepAlive: true, maxSockets: 50 })
const HTTPS_JSON_AGENT = new https.Agent({ keepAlive: true, maxSockets: 50 })

// Download pool: few long-lived streaming bodies. Isolated from the JSON pool
// so saturating it with large file downloads can't block interactive calls.
const HTTP_DOWNLOAD_AGENT = new http.Agent({ keepAlive: true, maxSockets: 8 })
const HTTPS_DOWNLOAD_AGENT = new https.Agent({ keepAlive: true, maxSockets: 8 })

// [daveey] Probe pool: the dispatch-time /api/health probe of a pooled remote
// (ensureHealthyPooledRemoteBackendForDispatch). On a slow link the JSON pool
// filled up (51 sockets to one tunnel on 2026-11-12), the probe queued behind
// it for minutes, failed, and tore down a tunnel that was answering fine.
// Its own tiny pool keeps the health verdict about the remote, not about our
// local socket queue.
const PROBE_POOL_MAX_SOCKETS = 2
const HTTP_PROBE_AGENT = new http.Agent({ keepAlive: true, maxSockets: PROBE_POOL_MAX_SOCKETS })
const HTTPS_PROBE_AGENT = new https.Agent({ keepAlive: true, maxSockets: PROBE_POOL_MAX_SOCKETS })

export type JsonRequestPool = 'json' | 'probe'

function jsonAgentFor(protocol: string, pool: JsonRequestPool = 'json') {
  if (pool === 'probe') {
    return protocol === 'https:' ? HTTPS_PROBE_AGENT : HTTP_PROBE_AGENT
  }

  return protocol === 'https:' ? HTTPS_JSON_AGENT : HTTP_JSON_AGENT
}

function downloadAgentFor(protocol) {
  return protocol === 'https:' ? HTTPS_DOWNLOAD_AGENT : HTTP_DOWNLOAD_AGENT
}

// Close pooled sockets so lingering keep-alive connections can't hold the
// process open (or leak FDs) across quit. Wired to app 'will-quit' in main.ts.
function destroyKeepaliveAgents() {
  for (const agent of [
    HTTP_JSON_AGENT,
    HTTPS_JSON_AGENT,
    HTTP_PROBE_AGENT,
    HTTPS_PROBE_AGENT,
    HTTP_DOWNLOAD_AGENT,
    HTTPS_DOWNLOAD_AGENT
  ]) {
    agent.destroy()
  }
}

function requestTimeoutError(timeoutMs: number): Error {
  return new Error(`Timed out connecting to Hermes backend after ${timeoutMs}ms`)
}

// Assemble a single-file multipart/form-data body (FastAPI `UploadFile`
// endpoints, e.g. kanban attachments). Hand-rolled because node's http has no
// FormData and the payload is one file — a dependency would be overkill.
function multipartBody(upload: any) {
  const boundary = `----hermes-${crypto.randomBytes(12).toString('hex')}`
  const filename = String(upload.filename || 'file').replace(/["\r\n]/g, '_')

  const body = Buffer.concat([
    Buffer.from(
      `--${boundary}\r\n` +
        `Content-Disposition: form-data; name="file"; filename="${filename}"\r\n` +
        `Content-Type: ${upload.contentType || 'application/octet-stream'}\r\n\r\n`
    ),
    Buffer.from(upload.bytes),
    Buffer.from(`\r\n--${boundary}--\r\n`)
  ])

  return { body, contentType: `multipart/form-data; boundary=${boundary}` }
}

/**
 * [daveey] Bound one JSON request by a TOTAL deadline, not only an idle one.
 *
 * `req.setTimeout` is a per-socket idle timer that starts only once the Agent
 * hands the request a socket, so time spent queued behind a full pool is
 * unbounded, and a response that trickles bytes never trips it. On a slow link
 * (2026-11-12, MacBook on a 1-2 Mbps guest network) that let a 10 s health
 * probe run 227 s and a down-gate trial hold its slot for 3.5 min.
 *
 * Call this BEFORE `http.request` so the clock starts at call time, then
 * `arm(req, reject)` right after: on expiry the caller's promise rejects with
 * the usual "Timed out connecting to Hermes backend" error FIRST (so down-gate
 * and retry classification are unchanged: a timeout is not a transient
 * transport error) and only then is the request destroyed, so a socket error
 * raised by the destroy can't win the race. The idle timeout is kept too.
 */
function startRequestDeadline(timeoutMs: number, now: () => number = Date.now) {
  const startedAt = now()

  return {
    arm(req: http.ClientRequest, onTimeout: (error: Error) => void): void {
      const expire = () => {
        const error = requestTimeoutError(timeoutMs)
        onTimeout(error)
        req.destroy(error)
      }

      const timer = setTimeout(expire, Math.max(0, timeoutMs - (now() - startedAt)))
      const clear = () => clearTimeout(timer)

      req.once('close', clear)
      req.once('response', (res: http.IncomingMessage) => res.once('close', clear))
      req.setTimeout(timeoutMs, expire)
    }
  }
}

// Transient transport errors: retry MAY be safe (subject to verb gating).
const TRANSIENT_CODES = new Set([
  'ECONNRESET',
  'ECONNREFUSED',
  'EPIPE',
  'ETIMEDOUT',
  'EAI_AGAIN',
  'ENOTFOUND',
  'EHOSTUNREACH',
  'ENETUNREACH'
])

// Errors that prove the request never reached the server: the TCP connection
// (or name resolution) failed outright, so nothing was submitted.
const NEVER_SENT_CODES = new Set(['ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'EHOSTUNREACH', 'ENETUNREACH'])

const IDEMPOTENT_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

function isIdempotentMethod(method) {
  return IDEMPOTENT_METHODS.has(String(method || 'GET').toUpperCase())
}

function isTransientTransportError(error) {
  if (!error) {
    return false
  }

  if (TRANSIENT_CODES.has(error.code)) {
    return true
  }

  const msg = String(error.message || '')

  return msg.includes('socket hang up') || msg.includes('read ECONNRESET')
}

/**
 * The verb-gated retry decision.
 *
 * @param error        the transport error from the failed attempt
 * @param method       HTTP verb of the request ('GET', 'POST', ...)
 * @param requestState per-attempt state; requestState.bodySent is set true by
 *                     the caller just BEFORE the first byte of the request is
 *                     flushed, so a `false` here proves nothing went out.
 */
function shouldRetryRequest(error, method, requestState: any = {}) {
  if (!isTransientTransportError(error)) {
    return false
  }

  if (isIdempotentMethod(method)) {
    return true
  }

  // Non-idempotent: only when the request provably never reached the server.
  if (NEVER_SENT_CODES.has(error && error.code)) {
    return true
  }

  if (requestState.bodySent === false) {
    return true
  }

  // Ambiguous (reset/hang-up after the body was flushed): the server may have
  // processed it. Surface the error rather than risk a double submit.
  return false
}

/**
 * Run `makeAttempt` with bounded retries under the policy above.
 *
 * `makeAttempt(requestState)` must return a Promise and should set
 * `requestState.bodySent = true` immediately before flushing the request
 * (before the first req.write()/req.end()). Each attempt gets a fresh state
 * object initialized to { bodySent: false }.
 */
async function withRetry(makeAttempt, options: any = {}) {
  const method = String(options.method || 'GET').toUpperCase()
  const maxRetries = Number.isInteger(options.maxRetries) ? options.maxRetries : 2

  const delayFn =
    options.delayFn || (attempt => new Promise(r => setTimeout(r, Math.min(200 * Math.pow(2, attempt), 2000))))

  let lastError

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    const requestState = { bodySent: false }

    try {
      return await makeAttempt(requestState)
    } catch (error) {
      lastError = error

      if (attempt < maxRetries && shouldRetryRequest(error, method, requestState)) {
        await delayFn(attempt)

        continue
      }

      throw error
    }
  }

  throw lastError
}

/**
 * The one error shape the REST helpers throw for an HTTP >= 400 response.
 *
 * `statusCode` is the structured contract every downstream classifier reads —
 * isGatewayAuthRejection (401/403 → reauth, never retried), isServerSideHttpError
 * (502/503/504 → Cloud-down), ensureNativeAccessToken's dead-refresh-token
 * check — and the "<status>: <body>" message keeps the legacy prefix readers
 * working. fetchJson used to build a bare Error here, so a native-bearer 401
 * reached the boot path as an anonymous transport failure: it was retried,
 * then classified as transient, and the renderer's boot-retry loop flickered
 * the Sign in overlay away (#95701). Shared by fetchJson, fetchPublicJson and
 * the OAuth-session fetch so the three paths cannot drift apart again.
 */
function httpStatusError(statusCode, text, statusMessage?) {
  const status = Number.isInteger(statusCode) && statusCode > 0 ? statusCode : 500
  const detail = String(text || statusMessage || '')
  const error: any = new Error(`${status}: ${detail}`)
  error.statusCode = status

  return error
}

/** Read side of httpStatusError: the HTTP status an error carries, NaN when it carries none. */
function readStatusCode(error: unknown): number {
  return Number(error && typeof error === 'object' ? (error as { statusCode?: unknown }).statusCode : NaN)
}

/**
 * The structured JSON body an httpStatusError carries after its "<status>: "
 * prefix, or null when the body was not a JSON object. NAS answers
 * `{ error: "<code>", ... }` on 4xx, and every reader of that code (the 409
 * org picker, the stale-team fallback) must parse the prefix the same way.
 */
function readJsonErrorBody(error: unknown): null | Record<string, unknown> {
  const message = error instanceof Error ? error.message : ''
  const start = message.indexOf('{')

  if (start < 0) {
    return null
  }

  try {
    const parsed: unknown = JSON.parse(message.slice(start))

    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : null
  } catch {
    return null
  }
}

/**
 * Error for a JSON endpoint that did not answer JSON. A 2xx HTML body is the
 * SPA index.html for an unregistered /api path, and downstream capability
 * probes (isMissingHealthEndpointError, gateway-rpc) key on the "endpoint is
 * likely missing" wording. A 3xx (callers never follow redirects, whatever
 * the body) is the request being bounced elsewhere: an access proxy sending
 * it to its login page, or a scheme / trailing-slash redirect when the saved
 * URL is off by that much — so it must neither carry that wording nor blame
 * the backend, and it names the Location when the server sent one.
 */
function htmlResponseError(url: string, statusCode: unknown, location?: unknown) {
  const status = Number(statusCode)

  if (status >= 300 && status < 400) {
    const target = typeof location === 'string' && location.trim() ? location.trim() : null

    const hint = isSchemeOrSlashRedirect(url, target)
      ? 'The saved gateway URL differs from the server by scheme or trailing slash; update it to match.'
      : 'This is usually an authentication proxy in front of the gateway; check the saved token and extra gateway headers.'

    return new Error(
      `Expected JSON from ${url} but the request was redirected (status ${statusCode})${target ? ` to ${target}` : ''}. ${hint}`
    )
  }

  return new Error(
    `Expected JSON from ${url} but got HTML (status ${statusCode}). The endpoint is likely missing on the Hermes backend.`
  )
}

function isSchemeOrSlashRedirect(requestUrl: string, location: null | string): boolean {
  if (!location) {
    return false
  }

  try {
    const from = new URL(requestUrl)
    const to = new URL(location, requestUrl)
    const strip = (value: string) => value.replace(/\/+$/, '')

    return (
      from.host === to.host &&
      from.search === to.search &&
      (from.protocol !== to.protocol || from.pathname !== to.pathname) &&
      strip(from.pathname) === strip(to.pathname)
    )
  } catch {
    return false
  }
}

export {
  destroyKeepaliveAgents,
  downloadAgentFor,
  htmlResponseError,
  httpStatusError,
  isIdempotentMethod,
  isTransientTransportError,
  jsonAgentFor,
  multipartBody,
  readJsonErrorBody,
  readStatusCode,
  requestTimeoutError,
  shouldRetryRequest,
  startRequestDeadline,
  withRetry
}
