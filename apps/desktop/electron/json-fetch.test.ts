import http from 'node:http'
import type { AddressInfo } from 'node:net'

import { afterAll, describe, expect, it } from 'vitest'

import {
  destroyKeepaliveAgents,
  PLUGIN_POOL_MAX_SOCKETS,
  pluginOfPath,
  PROBE_POOL_MAX_SOCKETS,
  requestDeadlineBudgetMs
} from './api-transport'
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

/** A loopback server on an ephemeral port: `listen()` resolves its base URL, `close()` drops every open connection. */
function loopbackServer(handler: http.RequestListener) {
  const server = http.createServer(handler)

  return {
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

/**
 * /busy reads the body, then keeps the connection active for BUSY_MS (a byte every 20 ms, so the idle timer never
 * fires) before answering with the byte count. /drop reads the body and destroys the socket without answering.
 */
function testServer() {
  const hits = { busy: 0, drop: 0 }

  const server = loopbackServer((req, res) => {
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

  return { hits, ...server }
}

afterAll(() => destroyKeepaliveAgents())

describe('fetchJson request pools', () => {
  /** Counts requests that reached the server and holds each open until `release()`. */
  function holdingServer() {
    const held: http.ServerResponse[] = []

    const server = loopbackServer((_req, res) => {
      held.push(res)
    })

    return {
      held,
      ...server,
      release() {
        for (const res of held.splice(0)) {
          res.writeHead(200, { 'Content-Type': 'application/json' })
          res.end('{"ok":true}')
        }
      }
    }
  }

  // One more concurrent call than the probe pool has sockets.
  const OVER_PROBE_LIMIT = PROBE_POOL_MAX_SOCKETS + 1
  const allOk = Array.from({ length: OVER_PROBE_LIMIT }, () => ({ ok: true }))

  const reached = async (srv: { held: unknown[] }, count: number) => {
    const until = Date.now() + 2_000

    while (srv.held.length < count && Date.now() < until) {
      await new Promise(resolve => setTimeout(resolve, 10))
    }

    // Give a queued request time to (wrongly) get a socket of its own.
    await new Promise(resolve => setTimeout(resolve, 100))

    return srv.held.length
  }

  it("pool: 'probe' rides the small probe pool, so one probe past its socket limit queues", async () => {
    const srv = holdingServer()
    const base = await srv.listen()

    try {
      const calls = allOk.map(() => fetchJsonWith(deps, `${base}/api/health`, 'token', { pool: 'probe' }))

      expect(await reached(srv, PROBE_POOL_MAX_SOCKETS)).toBe(PROBE_POOL_MAX_SOCKETS)
      srv.release()
      expect(await reached(srv, 1)).toBe(1)
      srv.release()
      await expect(Promise.all(calls)).resolves.toEqual(allOk)
    } finally {
      await srv.close()
    }
  })

  it('the default JSON pool is not held to the probe limit', async () => {
    const srv = holdingServer()
    const base = await srv.listen()

    try {
      const calls = allOk.map(() => fetchJsonWith(deps, `${base}/api/health`, 'token'))

      expect(await reached(srv, OVER_PROBE_LIMIT)).toBe(OVER_PROBE_LIMIT)
      srv.release()
      await expect(Promise.all(calls)).resolves.toEqual(allOk)
    } finally {
      await srv.close()
    }
  })
})

describe('fetchJson plugin pools', () => {
  // [daveey] 2026-11-13: the runners plugin kept ~11 slow /v1/fleet reads in flight per 15 s poll, each answered after
  // ~30 s; overlapping polls and their retries filled the 50-socket JSON pool, and kanban/board queued past its total
  // deadline. 60 such reads is more than the JSON pool holds, so on a shared pool the board read below can't get a
  // socket at all.
  const SLOW_PLUGIN_CALLS = 60
  const SLOW_RESPONSE_MS = 30_000
  const BOARD_DEADLINE_MS = 3_000

  /** /api/plugins/runners/* answers after SLOW_RESPONSE_MS (or at `release()`); every other path answers at once. */
  function slowPluginServer() {
    const slow: Array<{ res: http.ServerResponse; timer: ReturnType<typeof setTimeout> }> = []

    const answer = (res: http.ServerResponse) => {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end('{"ok":true}')
    }

    const server = loopbackServer((req, res) => {
      if (req.url?.startsWith('/api/plugins/runners/')) {
        const entry = { res, timer: setTimeout(() => answer(res), SLOW_RESPONSE_MS) }
        slow.push(entry)

        return
      }

      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ path: req.url }))
    })

    return {
      slow,
      ...server,
      release() {
        for (const { res, timer } of slow.splice(0)) {
          clearTimeout(timer)
          answer(res)
        }
      }
    }
  }

  it('one plugin with 30 s responses cannot starve a concurrent kanban/board read', async () => {
    const srv = slowPluginServer()
    const base = await srv.listen()

    const flood = Array.from({ length: SLOW_PLUGIN_CALLS }, () =>
      fetchJsonWith(deps, `${base}/api/plugins/runners/fleet`, 'token', { timeoutMs: SLOW_RESPONSE_MS * 2 })
    )

    try {
      // The flood holds every socket its pool will give it before the board read goes out.
      const until = Date.now() + 2_000

      while (srv.slow.length < Math.min(SLOW_PLUGIN_CALLS, 50) && Date.now() < until) {
        await new Promise(resolve => setTimeout(resolve, 10))
      }

      const startedAt = Date.now()

      await expect(
        fetchJsonWith(deps, `${base}/api/plugins/kanban/board?board=default`, 'token', { timeoutMs: BOARD_DEADLINE_MS })
      ).resolves.toEqual({ path: '/api/plugins/kanban/board?board=default' })
      expect(Date.now() - startedAt).toBeLessThan(BOARD_DEADLINE_MS)
      // A core route is not held behind the flood either.
      await expect(
        fetchJsonWith(deps, `${base}/api/status`, 'token', { timeoutMs: BOARD_DEADLINE_MS })
      ).resolves.toEqual({
        path: '/api/status'
      })
      // The flooding plugin holds at most its own share of sockets on this connection.
      expect(srv.slow.length).toBe(PLUGIN_POOL_MAX_SOCKETS)
    } finally {
      // Answer the held reads, then each queued one as it reaches the server, so the flood settles before close.
      const drain = setInterval(() => srv.release(), 5)
      await Promise.allSettled(flood)
      clearInterval(drain)
      await srv.close()
    }
  }, 20_000)

  it('names the plugin a path is addressed to, and only plugin paths', () => {
    expect(pluginOfPath('/api/plugins/kanban/board')).toBe('kanban')
    expect(pluginOfPath('/api/plugins/Hub-Sync/status')).toBe('hub-sync')
    expect(pluginOfPath('/api/plugins/runners')).toBe('runners')
    expect(pluginOfPath('/api/status')).toBeNull()
    expect(pluginOfPath('/api/pluginsx/kanban')).toBeNull()
    expect(pluginOfPath('')).toBeNull()
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
