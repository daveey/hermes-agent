/**
 * [daveey] The Electron main process's JSON REST helpers (fetchJson / fetchPublicJson), lifted out of main.ts so
 * vitest can drive them against a live local server without Electron. main.ts keeps thin `fetchJson` /
 * `fetchPublicJson` wrappers that pass its remote-header and native-gateway-grant sources in as `deps`.
 *
 * Transport policy (pools, retry gating, the total request deadline and its body-size budget) lives in
 * api-transport.ts; this module only builds and parses the requests.
 */

import http from 'node:http'
import https from 'node:https'

import {
  htmlResponseError,
  httpStatusError,
  jsonAgentFor,
  multipartBody,
  sendWithRequestDeadline,
  startRequestDeadline,
  withRetry
} from './api-transport'
import { decodeDeltaBody, readEncodingHeaders } from './delta-reads'
import { DEFAULT_FETCH_TIMEOUT_MS, resolveTimeoutMs } from './hardening'

/** What main.ts supplies: headers configured for remote gateways, and one-use native gateway grants. */
export interface JsonFetchDeps {
  remoteHeaders: (url: string) => Record<string, string>
  nativeHeaders: (descriptor: any, url: string, method: string, body: unknown) => Promise<null | Record<string, string>>
}

export function fetchJsonWith(deps: JsonFetchDeps, url: string, token: null | string, options: any = {}) {
  // Retry policy lives in api-transport.ts: idempotent verbs retry on any
  // transient transport error; POST/PUT/DELETE only when the request provably
  // never reached the server (see shouldRetryRequest) — never double-submit.
  return withRetry(
    async (requestState: any) => {
      // [daveey] Total per-attempt deadline from here: covers grant minting, Agent queue wait, connect and response.
      const timeoutMs = resolveTimeoutMs(options.timeoutMs, DEFAULT_FETCH_TIMEOUT_MS)
      const deadline = startRequestDeadline(timeoutMs)

      // Mint inside each retry: a grant authorizes exactly one HTTP request.
      const nativeHeaders = options.gatewayDescriptor
        ? await deps.nativeHeaders(
            options.gatewayDescriptor,
            url,
            options.method || 'GET',
            options.upload ? undefined : options.body
          )
        : null

      return new Promise((resolve, reject) => {
        const { body, contentType } = options.upload
          ? multipartBody(options.upload)
          : {
              body: options.body === undefined ? undefined : Buffer.from(JSON.stringify(options.body)),
              contentType: 'application/json'
            }

        const parsed = new URL(url)
        const client = parsed.protocol === 'https:' ? https : http
        // The dispatch health probe rides its own small pool (pool: 'probe') so a saturated JSON pool can't fail it;
        // a plugin route rides its plugin's own pool (api-transport PLUGIN_POOL_MAX_SOCKETS) so one plugin can't.
        const agent = jsonAgentFor(parsed.protocol, options.pool === 'probe' ? 'probe' : 'json', parsed.pathname)

        if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
          reject(new Error(`Unsupported Hermes backend URL protocol: ${parsed.protocol}`))

          return
        }

        // On the wire from here (api-transport sendWithRequestDeadline): the deadline is armed with the body's size,
        // and requestState.bodySent is set, so a later transport error no longer proves the server didn't process it
        // and non-idempotent verbs are not retried.
        sendWithRequestDeadline(
          deadline,
          () =>
            client.request(
              parsed,
              {
                agent,
                method: options.method || 'GET',
                headers: {
                  ...deps.remoteHeaders(url),
                  // [daveey] Transcript pages slim + gzip (display-transcript.ts); large reads as deltas (delta-reads.ts).
                  ...readEncodingHeaders(options.method, url),
                  ...(options.headers || {}),
                  'Content-Type': contentType,
                  ...(nativeHeaders || { 'X-Hermes-Session-Token': token }),
                  // RFC 8252 native flow authenticates the gated gateway with a bearer
                  // token instead of the loopback session-token header. When
                  // ``options.bearer`` is set we send Authorization: Bearer <token>;
                  // the gateway's OAuth gate verifies it via the provider stack with
                  // no cookie involved.
                  ...(options.bearer ? { Authorization: `Bearer ${options.bearer}` } : {}),
                  ...(body ? { 'Content-Length': String(body.length) } : {})
                }
              },
              res => {
                const chunks: Buffer[] = []
                res.on('error', reject)
                res.on('data', chunk => chunks.push(chunk))
                res.on('end', () => {
                  let text: string

                  try {
                    text = decodeDeltaBody(url, res, Buffer.concat(chunks))
                  } catch (error) {
                    reject(error)

                    return
                  }

                  if ((res.statusCode || 500) >= 400) {
                    reject(httpStatusError(res.statusCode, text, res.statusMessage))

                    return
                  }

                  // http.request never follows redirects, so any 3xx -- with an HTML
                  // login page, an empty body, whatever -- is the request bouncing
                  // off a proxy or a scheme/slash mismatch, never JSON.
                  if (res.statusCode >= 300) {
                    reject(htmlResponseError(url, res.statusCode, res.headers.location))

                    return
                  }

                  if (!text) {
                    resolve(null)

                    return
                  }

                  // A 2xx response whose body is HTML means the request fell through
                  // to the SPA index.html (e.g. an unregistered /api path). JSON.parse
                  // would throw an opaque `Unexpected token '<'` here, so surface a
                  // clear diagnostic with the offending URL instead.
                  const looksHtml = /^\s*<(?:!doctype|html)/i.test(text)
                  const contentType = String(res.headers['content-type'] || '')

                  if (looksHtml || contentType.includes('text/html')) {
                    reject(htmlResponseError(url, res.statusCode))

                    return
                  }

                  try {
                    resolve(JSON.parse(text))
                  } catch {
                    reject(new Error(`Invalid JSON from ${url} (status ${res.statusCode}): ${text.slice(0, 200)}`))
                  }
                })
              }
            ),
          { body, onError: reject, requestState }
        )
      })
    },
    { method: options.method || 'GET' }
  )
}

export function fetchPublicJsonWith(deps: JsonFetchDeps, url: string, options: any = {}) {
  // Credential-free JSON GET/POST for public gateway endpoints
  // (``/api/status``, ``/api/auth/providers``). Unlike ``fetchJson`` it sends
  // NO ``X-Hermes-Session-Token`` header — used by the auth-mode probe before
  // any credentials exist, and any time we must not leak a token to an
  // endpoint that doesn't need one.
  return withRetry(
    (requestState: any) =>
      new Promise((resolve, reject) => {
        const timeoutMs = resolveTimeoutMs(options.timeoutMs, DEFAULT_FETCH_TIMEOUT_MS)
        const deadline = startRequestDeadline(timeoutMs)
        const body = options.body === undefined ? undefined : Buffer.from(JSON.stringify(options.body))
        let parsed

        try {
          parsed = new URL(url)
        } catch (error) {
          reject(new Error(`Invalid URL: ${error.message}`))

          return
        }

        const client = parsed.protocol === 'https:' ? https : http
        const agent = jsonAgentFor(parsed.protocol, 'json', parsed.pathname)

        if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
          reject(new Error(`Unsupported Hermes backend URL protocol: ${parsed.protocol}`))

          return
        }

        // Past this point the request is on the wire — see fetchJson.
        sendWithRequestDeadline(
          deadline,
          () =>
            client.request(
              parsed,
              {
                agent,
                method: options.method || 'GET',
                headers: {
                  ...deps.remoteHeaders(url),
                  ...(options.headers || {}),
                  'Content-Type': 'application/json',
                  ...(body ? { 'Content-Length': String(body.length) } : {})
                }
              },
              res => {
                const chunks: Buffer[] = []
                res.on('data', chunk => chunks.push(chunk))
                res.on('end', () => {
                  const text = Buffer.concat(chunks).toString('utf8')

                  if ((res.statusCode || 500) >= 400) {
                    reject(httpStatusError(res.statusCode, text, res.statusMessage))

                    return
                  }

                  if (res.statusCode >= 300) {
                    reject(htmlResponseError(url, res.statusCode, res.headers.location))

                    return
                  }

                  if (!text) {
                    resolve(null)

                    return
                  }

                  const looksHtml = /^\s*<(?:!doctype|html)/i.test(text)
                  const contentType = String(res.headers['content-type'] || '')

                  if (looksHtml || contentType.includes('text/html')) {
                    reject(htmlResponseError(url, res.statusCode))

                    return
                  }

                  try {
                    resolve(JSON.parse(text))
                  } catch {
                    reject(new Error(`Invalid JSON from ${url} (status ${res.statusCode}): ${text.slice(0, 200)}`))
                  }
                })
              }
            ),
          { body, onError: reject, requestState }
        )
      }),
    { method: options.method || 'GET' }
  )
}
