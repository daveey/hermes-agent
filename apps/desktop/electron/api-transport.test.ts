/**
 * Unit + live-transport tests for the Electron main process's Hermes REST
 * retry policy (#92976 / PR #92977 salvage).
 *
 * The live tests run REAL node http servers that misbehave the way the
 * reported backend does (closing sockets under burst keep-alive traffic) and
 * prove two things end to end:
 *
 *  - idempotent GETs that die with ECONNRESET are retried and succeed, where
 *    a single bare attempt (pre-PR behavior) surfaces the raw reset;
 *  - a POST whose socket is reset AFTER the server processed it is NOT
 *    retried: the server-side hit counter stays at 1 and the error surfaces.
 */
import http from 'node:http'
import type { AddressInfo } from 'node:net'

import { afterAll, describe, expect, it } from 'vitest'

import {
  destroyKeepaliveAgents,
  htmlResponseError,
  httpStatusError,
  isIdempotentMethod,
  isTransientTransportError,
  jsonAgentFor,
  multipartBody,
  requestDeadlineBudgetMs,
  sendWithRequestDeadline,
  shouldRetryRequest,
  startRequestDeadline,
  withRetry
} from './api-transport'

function errWithCode(code: string, message = code): NodeJS.ErrnoException {
  const e: NodeJS.ErrnoException = new Error(message)
  e.code = code

  return e
}

afterAll(() => {
  destroyKeepaliveAgents()
})

describe('isTransientTransportError', () => {
  it('accepts transient socket-level codes and messages', () => {
    for (const code of ['ECONNRESET', 'ECONNREFUSED', 'EPIPE', 'ETIMEDOUT', 'ENOTFOUND', 'EAI_AGAIN']) {
      expect(isTransientTransportError(errWithCode(code))).toBe(true)
    }

    expect(isTransientTransportError(new Error('socket hang up'))).toBe(true)
    expect(isTransientTransportError(new Error('read ECONNRESET'))).toBe(true)
  })

  it('rejects non-transport errors', () => {
    expect(isTransientTransportError(new Error('404: not found'))).toBe(false)
    expect(isTransientTransportError(new Error('Invalid JSON from http://x'))).toBe(false)
    expect(isTransientTransportError(null)).toBe(false)
    expect(isTransientTransportError(undefined)).toBe(false)
  })
})

describe('isIdempotentMethod', () => {
  it.each([
    ['GET', true],
    ['get', true],
    ['HEAD', true],
    ['OPTIONS', true],
    ['POST', false],
    ['PUT', false],
    ['PATCH', false],
    ['DELETE', false],
    [undefined, true] // node http defaults omitted method to GET
  ])('%s -> %s', (method, expected) => {
    expect(isIdempotentMethod(method)).toBe(expected)
  })
})

describe('shouldRetryRequest truth table', () => {
  const reset = () => errWithCode('ECONNRESET', 'read ECONNRESET')
  const refused = () => errWithCode('ECONNREFUSED', 'connect ECONNREFUSED 127.0.0.1:1')
  const hangUp = () => new Error('socket hang up')

  it('GET: retries any transient error regardless of body state', () => {
    expect(shouldRetryRequest(reset(), 'GET', { bodySent: true })).toBe(true)
    expect(shouldRetryRequest(reset(), 'GET', { bodySent: false })).toBe(true)
    expect(shouldRetryRequest(hangUp(), 'HEAD', { bodySent: true })).toBe(true)
  })

  it('GET: never retries non-transport errors (HTTP 4xx/5xx surfaced as Error)', () => {
    expect(shouldRetryRequest(new Error('500: boom'), 'GET', { bodySent: true })).toBe(false)
  })

  it('POST: retries when the connection provably never happened', () => {
    expect(shouldRetryRequest(refused(), 'POST', { bodySent: false })).toBe(true)
    expect(shouldRetryRequest(refused(), 'POST', { bodySent: true })).toBe(true) // refused == nothing sent
    expect(shouldRetryRequest(errWithCode('ENOTFOUND'), 'PUT', { bodySent: false })).toBe(true)
  })

  it('POST: retries transient errors thrown before the body was flushed', () => {
    expect(shouldRetryRequest(reset(), 'POST', { bodySent: false })).toBe(true)
    expect(shouldRetryRequest(hangUp(), 'DELETE', { bodySent: false })).toBe(true)
  })

  it('POST: does NOT retry ambiguous resets after the body went out', () => {
    expect(shouldRetryRequest(reset(), 'POST', { bodySent: true })).toBe(false)
    expect(shouldRetryRequest(hangUp(), 'POST', { bodySent: true })).toBe(false)
    expect(shouldRetryRequest(errWithCode('EPIPE'), 'PUT', { bodySent: true })).toBe(false)
    expect(shouldRetryRequest(errWithCode('ETIMEDOUT'), 'DELETE', { bodySent: true })).toBe(false)
  })

  it('POST: conservative when request state is unknown', () => {
    // No bodySent flag at all — treat as "may have been sent", don't retry.
    expect(shouldRetryRequest(reset(), 'POST', {})).toBe(false)
    expect(shouldRetryRequest(reset(), 'POST')).toBe(false)
  })
})

describe('withRetry', () => {
  const noDelay = { delayFn: () => Promise.resolve() }

  it('retries a GET through transient failures and resolves', async () => {
    let attempts = 0

    const result = await withRetry(
      () => {
        attempts += 1

        if (attempts < 3) {
          return Promise.reject(errWithCode('ECONNRESET'))
        }

        return Promise.resolve('ok')
      },
      { method: 'GET', ...noDelay }
    )

    expect(result).toBe('ok')
    expect(attempts).toBe(3)
  })

  it('gives each attempt a fresh requestState', async () => {
    const seen: boolean[] = []
    let attempts = 0
    await withRetry(
      (state: any) => {
        seen.push(state.bodySent)
        state.bodySent = true
        attempts += 1

        if (attempts < 2) {
          return Promise.reject(errWithCode('ECONNREFUSED'))
        }

        return Promise.resolve(null)
      },
      { method: 'POST', ...noDelay }
    )
    expect(seen).toEqual([false, false])
  })

  it('does not retry a POST that failed after the body was flushed', async () => {
    let attempts = 0
    await expect(
      withRetry(
        (state: any) => {
          attempts += 1
          state.bodySent = true

          return Promise.reject(errWithCode('ECONNRESET', 'read ECONNRESET'))
        },
        { method: 'POST', ...noDelay }
      )
    ).rejects.toThrow('read ECONNRESET')
    expect(attempts).toBe(1)
  })

  it('retries a POST on ECONNREFUSED (never reached the server)', async () => {
    let attempts = 0
    await expect(
      withRetry(
        () => {
          attempts += 1

          return Promise.reject(errWithCode('ECONNREFUSED'))
        },
        { method: 'POST', maxRetries: 2, ...noDelay }
      )
    ).rejects.toThrow('ECONNREFUSED')
    expect(attempts).toBe(3)
  })

  it('bounds retries at maxRetries even for GET', async () => {
    let attempts = 0
    await expect(
      withRetry(
        () => {
          attempts += 1

          return Promise.reject(errWithCode('ECONNRESET'))
        },
        { method: 'GET', maxRetries: 2, ...noDelay }
      )
    ).rejects.toThrow()
    expect(attempts).toBe(3)
  })

  it('never retries non-transient errors', async () => {
    let attempts = 0
    await expect(
      withRetry(
        () => {
          attempts += 1

          return Promise.reject(new Error('500: internal'))
        },
        { method: 'GET', ...noDelay }
      )
    ).rejects.toThrow('500')
    expect(attempts).toBe(1)
  })
})

// ---------------------------------------------------------------------------
// LIVE transport tests against real misbehaving HTTP servers.
// ---------------------------------------------------------------------------

/** Minimal single-attempt GET mirroring the pre-PR fetchJson (no retry). */
function bareJsonGet(url: string): Promise<any> {
  return new Promise((resolve, reject) => {
    const req = http.request(new URL(url), { agent: jsonAgentFor('http:'), method: 'GET' }, res => {
      const chunks: Buffer[] = []
      res.on('error', reject)
      res.on('data', c => chunks.push(c))
      res.on('end', () => resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))))
    })

    req.on('error', reject)
    req.end()
  })
}

/** The head behavior: same request under the verb-gated retry policy. */
function retriedJsonGet(url: string): Promise<any> {
  return withRetry(() => bareJsonGet(url), { method: 'GET', delayFn: () => Promise.resolve() })
}

function listen(server: http.Server): Promise<string> {
  return new Promise(resolve => {
    server.listen(0, '127.0.0.1', () => {
      resolve(`http://127.0.0.1:${(server.address() as AddressInfo).port}`)
    })
  })
}

describe('live: GET burst against a server that resets keep-alive sockets', () => {
  it('bare attempts fail with ECONNRESET/hang-up; retried GETs all succeed', async () => {
    // Deterministic misbehavior: every other request gets its socket
    // destroyed instead of a response — the observable client-side effect of
    // a backend killing idle keep-alive sockets mid-burst.
    let hits = 0

    const server = http.createServer((req, res) => {
      hits += 1

      if (hits % 2 === 1) {
        req.socket.destroy()

        return
      }

      res.setHeader('content-type', 'application/json')
      res.end(JSON.stringify({ n: hits }))
    })

    const base = await listen(server)

    try {
      // BASE (pre-PR, single attempt): the burst surfaces raw transport errors.
      let baseFailures = 0

      for (let i = 0; i < 6; i++) {
        try {
          await bareJsonGet(`${base}/api/sessions`)
        } catch (error: any) {
          baseFailures += 1
          expect(isTransientTransportError(error)).toBe(true)
        }
      }

      expect(baseFailures).toBeGreaterThan(0)

      // HEAD (retry policy): the same burst fully succeeds. Sequential so the
      // server's alternating destroy/respond pattern is deterministic per
      // request (first attempt reset, retry served).
      for (let i = 0; i < 6; i++) {
        const r = await retriedJsonGet(`${base}/api/sessions`)
        expect(r).toHaveProperty('n')
      }
    } finally {
      server.close()
    }
  }, 20_000)
})

describe('live: POST reset after server-side processing', () => {
  it('does not double-submit: server hit count stays 1, error surfaces', async () => {
    // The server fully receives and "processes" the POST (counter increments),
    // then RSTs the socket before responding — the dangerous ambiguous case.
    let posts = 0

    const server = http.createServer((req, res) => {
      const chunks: Buffer[] = []
      req.on('data', c => chunks.push(c))
      req.on('end', () => {
        posts += 1 // processed: prompt submitted / session created
        req.socket.resetAndDestroy()
        void res
      })
    })

    const base = await listen(server)

    const postOnce = () =>
      withRetry(
        (state: any) =>
          new Promise((resolve, reject) => {
            const body = Buffer.from(JSON.stringify({ prompt: 'hello' }))

            const req = http.request(
              new URL(`${base}/api/prompt`),
              {
                agent: jsonAgentFor('http:'),
                method: 'POST',
                headers: { 'content-type': 'application/json', 'content-length': String(body.length) }
              },
              res => {
                res.resume()
                res.on('end', () => resolve(null))
              }
            )

            req.on('error', reject)
            state.bodySent = true
            req.write(body)
            req.end()
          }),
        { method: 'POST', delayFn: () => Promise.resolve() }
      )

    try {
      await expect(postOnce()).rejects.toSatisfy((error: any) => isTransientTransportError(error))
      expect(posts).toBe(1) // exactly one server-side submission — no retry
    } finally {
      server.close()
    }
  }, 20_000)
})

describe('httpStatusError', () => {
  it('carries the integer statusCode downstream classifiers read, plus the legacy "<status>: <body>" message', () => {
    const err = httpStatusError(401, '{"error":"session_expired"}', 'Unauthorized')

    expect(err).toBeInstanceOf(Error)
    expect(err.statusCode).toBe(401)
    expect(err.message).toBe('401: {"error":"session_expired"}')
  })

  it('falls back to the status message, then to an empty detail, when the body is empty', () => {
    expect(httpStatusError(503, '', 'Service Unavailable').message).toBe('503: Service Unavailable')
    expect(httpStatusError(403, '', undefined).message).toBe('403: ')
    expect(httpStatusError(403, '', undefined).statusCode).toBe(403)
  })

  it('normalizes a missing/invalid status to 500 exactly like the `res.statusCode || 500` guard', () => {
    expect(httpStatusError(undefined, 'boom').statusCode).toBe(500)
    expect(httpStatusError(undefined, 'boom').message).toBe('500: boom')
    expect(httpStatusError(0, 'boom').statusCode).toBe(500)
  })
})

describe('htmlResponseError', () => {
  it('names an auth redirect with its Location for 3xx and keeps the endpoint-missing capability wording for 2xx HTML', () => {
    const redirected = htmlResponseError(
      'https://gateway.example.com/api/profiles',
      302,
      'https://sso.example.com/login?next=%2Fapi%2Fprofiles'
    ).message

    expect(redirected).toContain('status 302')
    expect(redirected).toContain('to https://sso.example.com/login?next=%2Fapi%2Fprofiles')
    expect(redirected).toMatch(/authentication proxy/)
    expect(redirected).not.toContain('endpoint is likely missing')
    expect(htmlResponseError('https://gateway.example.com/api/profiles', 307).message).toMatch(
      /redirected \(status 307\)\. This is usually/
    )
    expect(htmlResponseError('https://gateway.example.com/api/missing', 200).message).toContain(
      'endpoint is likely missing'
    )
  })

  it('does not blame credentials when the redirect only fixes the scheme or a trailing slash', () => {
    for (const [url, location] of [
      ['http://gateway.example.com/api/profiles', 'https://gateway.example.com/api/profiles'],
      ['https://gateway.example.com/api/profiles', '/api/profiles/'],
      ['https://gateway.example.com/hermes/api/health', 'https://gateway.example.com/hermes/api/health/']
    ]) {
      const message = htmlResponseError(url, 301, location).message

      expect(message).toContain(`to ${location}`)
      expect(message).toMatch(/scheme or trailing slash/)
      expect(message).not.toMatch(/authentication proxy/)
    }

    expect(
      htmlResponseError('https://gateway.example.com/api/profiles', 302, 'https://gateway.example.com/login').message
    ).toMatch(/authentication proxy/)
  })
})

// ---------------------------------------------------------------------------
// [daveey] Total request deadline + dedicated probe pool (2026-11-12: MacBook on a
// 1-2 Mbps link held 51 sockets to the hub tunnel; requests queued in the full
// Agent with no bound, a 10 s health probe ran 227 s and tore down a healthy
// tunnel).
// ---------------------------------------------------------------------------

/** One GET through fetchJson's send path: deadline started first, then sendWithRequestDeadline opens and arms. */
function deadlineJsonGet(url: string, agent: http.Agent, timeoutMs: number): Promise<any> {
  return new Promise((resolve, reject) => {
    const deadline = startRequestDeadline(timeoutMs)

    sendWithRequestDeadline(
      deadline,
      () =>
        http.request(new URL(url), { agent, method: 'GET' }, res => {
          const chunks: Buffer[] = []
          res.on('error', reject)
          res.on('data', c => chunks.push(c))
          res.on('end', () => resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || 'null')))
        }),
      { onError: reject }
    )
  })
}

/** Settles with the outcome, or 'still pending' once `ms` passed (so a RED run fails instead of hanging). */
function settleWithin<T>(promise: Promise<T>, ms: number): Promise<{ error?: Error; pending?: true; value?: T }> {
  return Promise.race([
    promise.then(
      value => ({ value }),
      error => ({ error })
    ),
    new Promise<{ pending: true }>(resolve => setTimeout(() => resolve({ pending: true }), ms))
  ])
}

/** Server whose /hang requests never answer (held until cleanup) and whose /api/health answers at once. */
function hangingServer() {
  const held: http.ServerResponse[] = []

  const server = http.createServer((req, res) => {
    if (req.url === '/api/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end('{"ok":true}')

      return
    }

    if (req.url === '/trickle') {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.write(' ')
      const tick = setInterval(() => res.write(' '), 20)
      res.on('close', () => clearInterval(tick))
      held.push(res)

      return
    }

    held.push(res)
  })

  const close = async () => {
    for (const res of held) {
      res.destroy()
    }

    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  }

  return { close, held, server }
}

describe('live: total request deadline (startRequestDeadline)', () => {
  it('rejects a request queued behind a full Agent by its deadline, not after a socket frees up', async () => {
    const { close, held, server } = hangingServer()
    const base = await listen(server)
    const agent = new http.Agent({ keepAlive: true, maxSockets: 1 })

    try {
      // Occupy the only socket with a request the server never answers.
      const blocker = deadlineJsonGet(`${base}/hang`, agent, 10_000)
      blocker.catch(() => {})
      await new Promise<void>(resolve => {
        const wait = () => (held.length > 0 ? resolve() : setTimeout(wait, 5))
        wait()
      })

      const startedAt = performance.now()
      const outcome = await settleWithin(deadlineJsonGet(`${base}/api/health`, agent, 150), 1_500)

      expect(outcome.pending).toBeUndefined()
      expect(String(outcome.error?.message)).toBe('Timed out connecting to Hermes backend after 150ms')
      expect(performance.now() - startedAt).toBeLessThan(1_000)
    } finally {
      agent.destroy()
      await close()
    }
  })

  it('rejects a trickling response by its deadline (an idle timer alone never fires)', async () => {
    const { close, server } = hangingServer()
    const base = await listen(server)
    const agent = new http.Agent({ keepAlive: true, maxSockets: 4 })

    try {
      const outcome = await settleWithin(deadlineJsonGet(`${base}/trickle`, agent, 200), 1_500)

      expect(outcome.pending).toBeUndefined()
      expect(String(outcome.error?.message)).toBe('Timed out connecting to Hermes backend after 200ms')
    } finally {
      agent.destroy()
      await close()
    }
  })

  it('a request that answers in time is not destroyed when its deadline would have passed', async () => {
    const { close, server } = hangingServer()
    const base = await listen(server)
    const agent = new http.Agent({ keepAlive: true, maxSockets: 1 })

    try {
      await expect(deadlineJsonGet(`${base}/api/health`, agent, 100)).resolves.toEqual({ ok: true })
      await new Promise(resolve => setTimeout(resolve, 150))
      // The kept-alive socket survived (no late destroy): the next request reuses it and answers.
      await expect(deadlineJsonGet(`${base}/api/health`, agent, 100)).resolves.toEqual({ ok: true })
    } finally {
      agent.destroy()
      await close()
    }
  })
})

/** One POST through fetchJson's send path (sendWithRequestDeadline), which arms the deadline with the body it writes. */
function deadlineJsonPost(
  url: string,
  agent: http.Agent,
  timeoutMs: number,
  body: Buffer,
  requestState: { bodySent?: boolean } = {}
): Promise<any> {
  return new Promise((resolve, reject) => {
    const deadline = startRequestDeadline(timeoutMs)

    sendWithRequestDeadline(
      deadline,
      () =>
        http.request(
          new URL(url),
          { agent, method: 'POST', headers: { 'Content-Length': String(body.length) } },
          res => {
            const chunks: Buffer[] = []
            res.on('error', reject)
            res.on('data', c => chunks.push(c))
            res.on('end', () => resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || 'null')))
          }
        ),
      { body, onError: reject, requestState }
    )
  })
}

/**
 * /slow-upload reads the whole body, then keeps the connection busy for `busyMs` (a byte every 20 ms, so the idle
 * timer never fires) before answering with the byte count: on a loopback socket this stands in for an upload whose
 * transfer outlasts the JSON deadline. /silent-upload reads the body and then never answers. /stall-upload stops
 * reading after the first chunk and never answers: the client's writes back up in the socket buffers mid-transfer.
 */
function uploadServer(busyMs: number) {
  const received = { stalled: 0 }

  const server = http.createServer((req, res) => {
    let bytes = 0

    if (req.url === '/stall-upload') {
      req.once('data', chunk => {
        received.stalled += chunk.length
        req.pause()
      })

      return
    }

    req.on('data', chunk => (bytes += chunk.length))
    req.on('end', () => {
      if (req.url === '/silent-upload') {
        return
      }

      res.writeHead(200, { 'Content-Type': 'application/json' })
      const tick = setInterval(() => res.write(' '), 20)
      setTimeout(() => {
        clearInterval(tick)
        res.end(JSON.stringify({ bytes }))
      }, busyMs)
    })
  })

  const close = async () => {
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  }

  return { close, received, server }
}

describe('total request deadline: request bodies (uploads)', () => {
  it('budgets the body at 1 Mbit/s on top of the timeout, so a 25 MB attachment on a 2 Mbps link fits', () => {
    expect(requestDeadlineBudgetMs(30_000)).toBe(30_000)
    expect(requestDeadlineBudgetMs(30_000, 0)).toBe(30_000)
    expect(requestDeadlineBudgetMs(30_000, 1_000)).toBe(30_008)

    const attachment = 25 * 1024 * 1024
    const secondsAt2Mbps = (attachment * 8) / 2_000_000
    expect(secondsAt2Mbps).toBeGreaterThan(100)
    expect(requestDeadlineBudgetMs(30_000, attachment)).toBe(30_000 + Math.ceil(attachment / 125))
    expect(requestDeadlineBudgetMs(30_000, attachment)).toBeGreaterThan(30_000 + 2 * secondsAt2Mbps * 1_000)
  })

  it('an upload whose transfer outlasts the JSON timeout completes within its body budget', async () => {
    const { close, server } = uploadServer(400)
    const base = await listen(server)
    const agent = new http.Agent({ keepAlive: true, maxSockets: 4 })
    const body = Buffer.alloc(256 * 1024, 7) // +2098 ms of budget at 125 bytes/ms

    try {
      const outcome = await settleWithin(deadlineJsonPost(`${base}/slow-upload`, agent, 150, body), 3_000)

      expect(outcome.error).toBeUndefined()
      expect(outcome.value).toEqual({ bytes: body.length })
    } finally {
      agent.destroy()
      await close()
    }
  })

  it('the same transfer still times out when the request carries no body allowance', async () => {
    const { close, server } = uploadServer(400)
    const base = await listen(server)
    const agent = new http.Agent({ keepAlive: true, maxSockets: 4 })

    try {
      const outcome = await settleWithin(deadlineJsonPost(`${base}/slow-upload`, agent, 150, Buffer.alloc(0)), 3_000)

      expect(String(outcome.error?.message)).toBe('Timed out connecting to Hermes backend after 150ms')
    } finally {
      agent.destroy()
      await close()
    }
  })

  it('a stalled upload is still cut by the idle timer at the caller timeout, not the body budget', async () => {
    const { close, server } = uploadServer(0)
    const base = await listen(server)
    const agent = new http.Agent({ keepAlive: true, maxSockets: 4 })
    const body = Buffer.alloc(256 * 1024, 7)

    try {
      const startedAt = performance.now()
      const outcome = await settleWithin(deadlineJsonPost(`${base}/silent-upload`, agent, 150, body), 3_000)

      expect(String(outcome.error?.message)).toBe('Timed out connecting to Hermes backend after 150ms')
      expect(performance.now() - startedAt).toBeLessThan(1_500)
    } finally {
      agent.destroy()
      await close()
    }
  })

  it('an upload stalled mid-transfer (server stops reading, socket backpressure) is cut by the idle timer', async () => {
    const { close, received, server } = uploadServer(0)
    const base = await listen(server)
    const agent = new http.Agent({ keepAlive: true, maxSockets: 4 })
    // A 32 MiB multipart attachment: far more than loopback socket buffers hold, so the transfer stalls partway.
    const { body } = multipartBody({ bytes: Buffer.alloc(32 * 1024 * 1024, 7), filename: 'big.bin' })
    // Its total budget is ~268 s: only the 150 ms idle timer can settle this inside the test's window.
    expect(requestDeadlineBudgetMs(150, body.length)).toBeGreaterThan(200_000)

    try {
      const startedAt = performance.now()
      const outcome = await settleWithin(deadlineJsonPost(`${base}/stall-upload`, agent, 150, body), 3_000)

      expect(outcome.pending).toBeUndefined()
      expect(String(outcome.error?.message)).toBe('Timed out connecting to Hermes backend after 150ms')
      expect(performance.now() - startedAt).toBeLessThan(1_500)
      // The server really stopped partway: it took one chunk of the body, not the whole upload.
      expect(received.stalled).toBeGreaterThan(0)
      expect(received.stalled).toBeLessThan(body.length)
    } finally {
      agent.destroy()
      await close()
    }
  })
})

describe('sendWithRequestDeadline (fetchJson / fetchPublicJson send path)', () => {
  it('arms the deadline with the byte size of the body it writes, after the deadline started', () => {
    const order: string[] = []
    const armed: Array<number | undefined> = []
    const writes: Buffer[] = []

    const deadline = {
      arm: (_req: http.ClientRequest, _onTimeout: (error: Error) => void, bodyBytes?: number) => {
        order.push('arm')
        armed.push(bodyBytes)
      }
    } as ReturnType<typeof startRequestDeadline>

    const fakeRequest = () => {
      order.push('open')

      return {
        end: () => order.push('end'),
        on: () => undefined,
        write: (chunk: Buffer) => writes.push(chunk)
      } as unknown as http.ClientRequest
    }

    const requestState: { bodySent?: boolean } = {}
    const body = Buffer.from('é'.repeat(10)) // 20 bytes, 10 characters: the budget counts bytes
    sendWithRequestDeadline(deadline, fakeRequest, { body, onError: () => {}, requestState })

    expect(order).toEqual(['open', 'arm', 'end'])
    expect(armed).toEqual([20])
    expect(writes).toEqual([body])
    expect(requestState.bodySent).toBe(true)

    // No body: armed with no allowance, nothing written.
    sendWithRequestDeadline(deadline, fakeRequest, { onError: () => {} })
    expect(armed).toEqual([20, undefined])
    expect(writes).toHaveLength(1)
  })

  it('a body sent through it gets its upload allowance end to end; the same wait with no body times out', async () => {
    const { close, server } = uploadServer(400)
    const base = await listen(server)
    const agent = new http.Agent({ keepAlive: true, maxSockets: 4 })
    const body = Buffer.alloc(256 * 1024, 7) // +2098 ms of budget at 125 bytes/ms
    const requestState: { bodySent?: boolean } = {}

    try {
      const sized = await settleWithin(deadlineJsonPost(`${base}/slow-upload`, agent, 150, body, requestState), 3_000)
      expect(sized.error).toBeUndefined()
      expect(sized.value).toEqual({ bytes: body.length })
      expect(requestState.bodySent).toBe(true)

      const empty = await settleWithin(deadlineJsonPost(`${base}/slow-upload`, agent, 150, Buffer.alloc(0)), 3_000)
      expect(String(empty.error?.message)).toBe('Timed out connecting to Hermes backend after 150ms')
    } finally {
      agent.destroy()
      await close()
    }
  })
})

describe('live: dispatch probe pool', () => {
  it('the probe answers while the JSON pool is saturated with hung requests to the same tunnel', async () => {
    const { close, held, server } = hangingServer()
    const base = await listen(server)
    const jsonPool = jsonAgentFor('http:')
    const hung: Promise<unknown>[] = []

    try {
      for (let i = 0; i < 50; i += 1) {
        const pending = deadlineJsonGet(`${base}/hang`, jsonPool, 10_000)
        pending.catch(() => {})
        hung.push(pending)
      }

      await new Promise<void>(resolve => {
        const wait = () => (held.length >= 50 ? resolve() : setTimeout(wait, 5))
        wait()
      })

      // Control: the JSON pool is full, so a health check there just queues.
      const queued = await settleWithin(deadlineJsonGet(`${base}/api/health`, jsonPool, 200), 1_500)
      expect(String(queued.error?.message)).toContain('Timed out connecting to Hermes backend')

      // The dispatch probe's own pool is not behind that queue.
      const probe = await settleWithin(
        deadlineJsonGet(`${base}/api/health`, jsonAgentFor('http:', 'probe'), 1_000),
        1_500
      )

      expect(probe.error).toBeUndefined()
      expect(probe.value).toEqual({ ok: true })
    } finally {
      await close()
    }
  })
})
