import http from 'node:http'
import type { AddressInfo } from 'node:net'

import { afterAll, describe, expect, it } from 'vitest'

import { destroyKeepaliveAgents, requestDeadlineBudgetMs } from './api-transport'
import { fetchJsonWith, fetchPublicJsonWith, type JsonFetchDeps } from './json-fetch'

/**
 * fetchJson / fetchPublicJson themselves (json-fetch.ts, which main.ts wraps), driven against a live loopback server.
 * api-transport.test.ts proves sendWithRequestDeadline; these prove both helpers actually send through it: a large
 * body gets the body-scaled total budget, and once the body is on the wire a POST is not replayed.
 */

const deps: JsonFetchDeps = {
  remoteHeaders: () => ({}),
  nativeHeaders: async () => null
}

const TIMEOUT_MS = 200
// ~125 KB of JSON: at 125 bytes/ms the total budget grows by ~1 s, well past BUSY_MS.
const LARGE = { blob: 'x'.repeat(125_000) }
const SMALL = { blob: 'x' }
// The server stays busy this long after reading the body: longer than TIMEOUT_MS, shorter than the scaled budget.
const BUSY_MS = 600

/**
 * /busy reads the body, then keeps the connection active for BUSY_MS (a byte every 20 ms, so the idle timer never
 * fires) before answering with the byte count. /drop reads the body and destroys the socket without answering.
 */
function testServer() {
  const hits = { busy: 0, drop: 0 }

  const server = http.createServer((req, res) => {
    let bytes = 0
    req.on('data', chunk => (bytes += chunk.length))
    req.on('end', () => {
      if (req.url === '/drop') {
        hits.drop += 1
        req.socket.destroy()

        return
      }

      hits.busy += 1
      res.writeHead(200, { 'Content-Type': 'application/json' })
      const tick = setInterval(() => res.write(' '), 20)
      setTimeout(() => {
        clearInterval(tick)
        res.end(JSON.stringify({ bytes }))
      }, BUSY_MS)
    })
  })

  return {
    hits,
    async listen(): Promise<string> {
      await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))

      return `http://127.0.0.1:${(server.address() as AddressInfo).port}`
    },
    close: () =>
      new Promise<void>(resolve => {
        server.closeAllConnections()
        server.close(() => resolve())
      })
  }
}

afterAll(() => destroyKeepaliveAgents())

describe('fetchJson request pools', () => {
  /** Counts requests that reached the server and holds each open until `release()`. */
  function holdingServer() {
    const held: http.ServerResponse[] = []

    const server = http.createServer((_req, res) => {
      held.push(res)
    })

    return {
      held,
      async listen(): Promise<string> {
        await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))

        return `http://127.0.0.1:${(server.address() as AddressInfo).port}`
      },
      release() {
        for (const res of held.splice(0)) {
          res.writeHead(200, { 'Content-Type': 'application/json' })
          res.end('{"ok":true}')
        }
      },
      close: () =>
        new Promise<void>(resolve => {
          server.closeAllConnections()
          server.close(() => resolve())
        })
    }
  }

  const reached = async (srv: { held: unknown[] }, count: number) => {
    const until = Date.now() + 2_000

    while (srv.held.length < count && Date.now() < until) {
      await new Promise(resolve => setTimeout(resolve, 10))
    }

    // Give a queued request time to (wrongly) get a socket of its own.
    await new Promise(resolve => setTimeout(resolve, 100))

    return srv.held.length
  }

  it("pool: 'probe' rides the small probe pool, so a third concurrent probe queues", async () => {
    const srv = holdingServer()
    const base = await srv.listen()

    try {
      const calls = [1, 2, 3].map(() => fetchJsonWith(deps, `${base}/api/health`, 'token', { pool: 'probe' }))

      expect(await reached(srv, 2)).toBe(2)
      srv.release()
      expect(await reached(srv, 1)).toBe(1)
      srv.release()
      await expect(Promise.all(calls)).resolves.toEqual([{ ok: true }, { ok: true }, { ok: true }])
    } finally {
      await srv.close()
    }
  })

  it('the default JSON pool is not held to the probe limit', async () => {
    const srv = holdingServer()
    const base = await srv.listen()

    try {
      const calls = [1, 2, 3].map(() => fetchJsonWith(deps, `${base}/api/health`, 'token'))

      expect(await reached(srv, 3)).toBe(3)
      srv.release()
      await expect(Promise.all(calls)).resolves.toEqual([{ ok: true }, { ok: true }, { ok: true }])
    } finally {
      await srv.close()
    }
  })
})

const helpers = {
  fetchJson: (url: string, options: any) => fetchJsonWith(deps, url, 'session-token', options),
  fetchPublicJson: (url: string, options: any) => fetchPublicJsonWith(deps, url, options)
}

describe.each(Object.entries(helpers))('%s total deadline and retry state', (_name, fetchHelper) => {
  it('gives a large body the body-scaled budget: a POST outlasting the base deadline still answers', async () => {
    const srv = testServer()
    const base = await srv.listen()

    try {
      const answer = (await fetchHelper(`${base}/busy`, {
        method: 'POST',
        body: LARGE,
        timeoutMs: TIMEOUT_MS
      })) as { bytes: number }

      expect(answer.bytes).toBe(Buffer.byteLength(JSON.stringify(LARGE)))
    } finally {
      await srv.close()
    }
  })

  it('a small body gets only its own few ms on top: the same busy server times it out at that budget', async () => {
    const srv = testServer()
    const base = await srv.listen()
    const budgetMs = requestDeadlineBudgetMs(TIMEOUT_MS, Buffer.byteLength(JSON.stringify(SMALL)))

    try {
      expect(budgetMs).toBeLessThan(BUSY_MS)
      await expect(fetchHelper(`${base}/busy`, { method: 'POST', body: SMALL, timeoutMs: TIMEOUT_MS })).rejects.toThrow(
        `Timed out connecting to Hermes backend after ${budgetMs}ms`
      )
    } finally {
      await srv.close()
    }
  })

  it('marks the body sent: a POST dropped after the server read it is not replayed', async () => {
    const srv = testServer()
    const base = await srv.listen()

    try {
      await expect(fetchHelper(`${base}/drop`, { method: 'POST', body: SMALL, timeoutMs: 2_000 })).rejects.toThrow(
        /socket hang up|ECONNRESET/
      )
      expect(srv.hits.drop).toBe(1)
    } finally {
      await srv.close()
    }
  })

  it('a dropped GET is still retried (idempotent), so the drop above is the bodySent gate, not a dead retry loop', async () => {
    const srv = testServer()
    const base = await srv.listen()

    try {
      await expect(fetchHelper(`${base}/drop`, { timeoutMs: 2_000 })).rejects.toThrow(/socket hang up|ECONNRESET/)
      expect(srv.hits.drop).toBe(3)
    } finally {
      await srv.close()
    }
  })
})
