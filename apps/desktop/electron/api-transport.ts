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

// [daveey] A pooled socket idle this long is closed by us, never reused. The backend closes idle
// keep-alive connections after KEEP_ALIVE_IDLE_S (hermes_cli/web_server_transport.py, 120 s); if the
// server closes first, its FIN crosses the SSH tunnel after we have already written the next request
// into that socket: `socket hang up`, and one of those marks the whole remote down (hermes-hub
// 2026-11-13, uvicorn's 5 s default: 197 such races in 7 min). Closing well before the server keeps
// that race from ever starting. Node destroys a free socket at the agent `timeout`; an in-flight
// request has its own deadline (startRequestDeadline) and is not cut by it.
export const CLIENT_KEEPALIVE_IDLE_MS = 30_000

/** A keep-alive agent whose free sockets close after *idleMs* idle (the client closes, not the server). */
export function keepAliveAgent<T extends typeof http | typeof https>(
  mod: T,
  maxSockets: number,
  idleMs: number = CLIENT_KEEPALIVE_IDLE_MS
): T extends typeof https ? https.Agent : http.Agent {
  return new mod.Agent({ keepAlive: true, maxSockets, timeout: idleMs }) as any
}

// JSON pool: many small concurrent calls (session lists, config, prompts).
const HTTP_JSON_AGENT = keepAliveAgent(http, 50)
const HTTPS_JSON_AGENT = keepAliveAgent(https, 50)

// Download pool: few long-lived streaming bodies. Isolated from the JSON pool
// so saturating it with large file downloads can't block interactive calls.
const HTTP_DOWNLOAD_AGENT = keepAliveAgent(http, 8)
const HTTPS_DOWNLOAD_AGENT = keepAliveAgent(https, 8)

// [daveey] Probe pool: the dispatch-time /api/health probe of a pooled remote
// (ensureHealthyPooledRemoteBackendForDispatch). On a slow link the JSON pool
// filled up (51 sockets to one tunnel on 2026-11-12), the probe queued behind
// it for minutes, failed, and tore down a tunnel that was answering fine.
// Its own tiny pool keeps the health verdict about the remote, not about our
// local socket queue.
export const PROBE_POOL_MAX_SOCKETS = 2
const HTTP_PROBE_AGENT = keepAliveAgent(http, PROBE_POOL_MAX_SOCKETS)
const HTTPS_PROBE_AGENT = keepAliveAgent(https, PROBE_POOL_MAX_SOCKETS)

export type JsonRequestPool = 'json' | 'probe'

// [daveey] Plugin pools: every `/api/plugins/<id>/...` JSON call rides a pool of its own plugin, at most
// PLUGIN_POOL_MAX_SOCKETS sockets per connection (a Node agent caps sockets per origin, and each connection is its
// own origin). On 2026-11-13 the runners plugin kept 11 slow (30 s) /v1/fleet reads in flight per 15 s poll; they
// filled the shared 50-socket JSON pool and every other call (kanban/board, hub-sync/status) queued past its total
// deadline: 2803 timeouts in 30 min. Now a plugin that floods only queues behind itself: core calls keep the JSON
// pool and each other plugin, kanban included, keeps its own sockets.
export const PLUGIN_POOL_MAX_SOCKETS = 8
const PLUGIN_PATH = /^\/api\/plugins\/([^/?#]+)/
const PLUGIN_AGENTS = new Map<string, http.Agent>()

/** The plugin id a request path is addressed to (`/api/plugins/<id>/...`), or null for a core route. */
export function pluginOfPath(pathname: string): null | string {
  const match = PLUGIN_PATH.exec(pathname || '')

  return match ? match[1].toLowerCase() : null
}

function pluginAgentFor(protocol: string, plugin: string): http.Agent {
  const key = `${protocol === 'https:' ? 'https' : 'http'}:${plugin}`
  let agent = PLUGIN_AGENTS.get(key)

  if (!agent) {
    agent = keepAliveAgent(protocol === 'https:' ? https : http, PLUGIN_POOL_MAX_SOCKETS)
    PLUGIN_AGENTS.set(key, agent)
  }

  return agent
}

/**
 * The agent for one JSON request. `pathname` (the request URL's path) routes a plugin route to that plugin's own
 * pool; the probe pool and core routes are unchanged.
 */
function jsonAgentFor(protocol: string, pool: JsonRequestPool = 'json', pathname = '') {
  if (pool === 'probe') {
    return protocol === 'https:' ? HTTPS_PROBE_AGENT : HTTP_PROBE_AGENT
  }

  const plugin = pluginOfPath(pathname)

  if (plugin) {
    return pluginAgentFor(protocol, plugin)
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
    HTTPS_DOWNLOAD_AGENT,
    ...PLUGIN_AGENTS.values()
  ]) {
    agent.destroy()
  }

  PLUGIN_AGENTS.clear()
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

// [daveey] Slowest upload throughput a request body is budgeted for: 1 Mbit/s
// (125 bytes per ms). The 2026-11-12 guest link ran ~1-2 Mbps, where a 25 MB
// attachment takes 100-200 s, far past the 30 s JSON deadline.
const REQUEST_BODY_MIN_BYTES_PER_MS = 125

/** Total deadline for one attempt: the caller's timeout plus the time its body needs at the minimum throughput. */
function requestDeadlineBudgetMs(timeoutMs: number, bodyBytes = 0): number {
  return timeoutMs + Math.ceil(Math.max(0, bodyBytes) / REQUEST_BODY_MIN_BYTES_PER_MS)
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
 *
 * Pass the request body's size to `arm`: the total budget grows by the time
 * that body needs at REQUEST_BODY_MIN_BYTES_PER_MS (requestDeadlineBudgetMs),
 * so a large upload (kanban attachments, up to 25 MB) on a slow link is not
 * cut at the JSON budget. The idle timer still catches a stalled upload.
 */
function startRequestDeadline(timeoutMs: number, now: () => number = Date.now, onDeadline?: () => void) {
  const startedAt = now()

  return {
    arm(req: http.ClientRequest, onTimeout: (error: Error) => void, bodyBytes = 0): void {
      const budgetMs = requestDeadlineBudgetMs(timeoutMs, bodyBytes)

      const expire = (afterMs: number) => {
        onDeadline?.()
        const error = requestTimeoutError(afterMs)
        onTimeout(error)
        req.destroy(error)
      }

      const timer = setTimeout(() => expire(budgetMs), Math.max(0, budgetMs - (now() - startedAt)))
      const clear = () => clearTimeout(timer)

      req.once('close', clear)
      req.once('response', (res: http.IncomingMessage) => res.once('close', clear))
      // The idle timer stays at the caller's timeout whatever the body size: a stalled upload still fails fast.
      req.setTimeout(timeoutMs, () => expire(timeoutMs))
    }
  }
}

type RequestDeadline = ReturnType<typeof startRequestDeadline>

/**
 * [daveey] Put one JSON request on the wire under its already-started total deadline (fetchJson / fetchPublicJson).
 *
 * Taking the started deadline and an `open` callback (the `http.request` call) makes the ordering structural: the
 * clock started before the request was created. The body is passed once and used both to arm the deadline (its byte
 * size grows the total budget) and as what is written, so the budget always matches the bytes sent. Marks
 * `requestState.bodySent` before writing: from here a transport error can't prove the server didn't process it.
 */
function sendWithRequestDeadline(
  deadline: RequestDeadline,
  open: () => http.ClientRequest,
  {
    body,
    onError,
    requestState,
    observeRequest
  }: {
    body?: Buffer
    onError: (error: Error) => void
    requestState?: { bodySent?: boolean }
    observeRequest?: (req: http.ClientRequest) => void
  }
): http.ClientRequest {
  const req = open()
  observeRequest?.(req)

  req.on('error', onError)
  deadline.arm(req, onError, body?.length)

  if (requestState) {
    requestState.bodySent = true
  }

  if (body) {
    req.write(body)
  }

  req.end()

  return req
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
  'ENETUNREACH',
  // [daveey] delta-reads.ts: a delta response that could not be decoded. The copy it needed is dropped, so the
  // retry asks without a base and gets the whole body. Only GETs ask for deltas.
  'HERMES_DELTA_RETRY'
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
  requestDeadlineBudgetMs,
  requestTimeoutError,
  sendWithRequestDeadline,
  shouldRetryRequest,
  startRequestDeadline,
  withRetry
}
