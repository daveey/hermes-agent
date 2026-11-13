/**
 * [daveey] The keep-alive close race behind the hermes-hub flapping (2026-11-13), reproduced live.
 *
 * A backend that closes an idle keep-alive connection sends a FIN that reaches Desktop only after the SSH tunnel
 * carries it across. A request written into the pooled socket in that window dies with `socket hang up`, and the
 * remote-down gate then marks the whole remote down. On the hub, uvicorn's 5 s idle close produced 197 such races
 * in 7 minutes (tcpdump of :9147). The fix makes the client side close first: every pooled agent closes a free
 * socket after CLIENT_KEEPALIVE_IDLE_MS, well under the backend's KEEP_ALIVE_IDLE_S.
 *
 * The harness puts a TCP relay that delays the server's close by FIN_DELAY_MS (the tunnel) between a uvicorn-like
 * server with a short idle close and the client. Bare GETs, no retries, so each race surfaces.
 */
import { readFileSync } from 'node:fs'
import http from 'node:http'
import net from 'node:net'
import type { AddressInfo } from 'node:net'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { CLIENT_KEEPALIVE_IDLE_MS, downloadAgentFor, jsonAgentFor, keepAliveAgent } from './api-transport'

const SERVER_IDLE_MS = 200
const FIN_DELAY_MS = 300
// The next request goes out after the server closed but before its FIN crossed the relay.
const GAP_MS = 320

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

/**
 * Like uvicorn: answers each request with no `Keep-Alive: timeout=` hint (Node's own http server sends one, and
 * the client would honour it) and closes the connection after SERVER_IDLE_MS idle.
 */
function uvicornLikeServer(): net.Server {
  return net.createServer((socket: net.Socket) => {
    let idle: NodeJS.Timeout | undefined
    let buf = ''

    const arm = () => {
      clearTimeout(idle)
      idle = setTimeout(() => socket.destroy(), SERVER_IDLE_MS)
    }

    arm()
    socket.on('data', (chunk: Buffer) => {
      buf += chunk.toString('latin1')

      while (buf.includes('\r\n\r\n')) {
        buf = buf.slice(buf.indexOf('\r\n\r\n') + 4)
        socket.write('HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 11\r\n\r\n{"ok":true}')
      }

      arm()
    })
    socket.on('error', () => {})
    socket.on('close', () => clearTimeout(idle))
  })
}

async function tunnelledServer() {
  const server = uvicornLikeServer()
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const upstreamPort = (server.address() as AddressInfo).port

  const relay = net.createServer((client: net.Socket) => {
    const upstream = net.connect(upstreamPort, '127.0.0.1')
    client.on('data', (chunk: Buffer) => upstream.writable && upstream.write(chunk))
    upstream.on('data', (chunk: Buffer) => client.write(chunk))
    upstream.on('close', () => setTimeout(() => client.end(), FIN_DELAY_MS))
    client.on('close', () => upstream.destroy())
    client.on('error', () => upstream.destroy())
    upstream.on('error', () => {})
  })

  await new Promise<void>(resolve => relay.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${(relay.address() as AddressInfo).port}`

  return {
    base,
    close: async () => {
      await new Promise<void>(resolve => server.close(() => resolve()))
      await new Promise<void>(resolve => relay.close(() => resolve()))
    }
  }
}

function get(url: string, agent: http.Agent): Promise<string> {
  return new Promise((resolve, reject) => {
    const req = http.get(url, { agent }, (res: http.IncomingMessage) => {
      const chunks: Buffer[] = []
      res.on('data', (c: Buffer) => chunks.push(c))
      res.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    })

    req.on('error', reject)
  })
}

async function pollWithGaps(base: string, agent: http.Agent, rounds = 3) {
  let failures = 0

  for (let i = 0; i < rounds; i++) {
    try {
      await get(`${base}/api/status`, agent)
    } catch {
      failures += 1
    }

    await sleep(GAP_MS)
  }

  return failures
}

describe('live: keep-alive close race through a tunnel', () => {
  it('an agent that keeps idle sockets forever writes into server-closed sockets (socket hang up)', async () => {
    const srv = await tunnelledServer()
    const agent = new http.Agent({ keepAlive: true, maxSockets: 4 })

    try {
      expect(await pollWithGaps(srv.base, agent)).toBeGreaterThan(0)
    } finally {
      agent.destroy()
      await srv.close()
    }
  }, 15_000)

  it('keepAliveAgent closes its idle socket first, so the same polling never races the server close', async () => {
    const srv = await tunnelledServer()
    const agent = keepAliveAgent(http, 4, SERVER_IDLE_MS / 2)

    try {
      expect(await pollWithGaps(srv.base, agent, 5)).toBe(0)
    } finally {
      agent.destroy()
      await srv.close()
    }
  }, 15_000)
})

describe('pooled agents close idle sockets before the backend does', () => {
  it('every pool uses the client idle timeout', () => {
    const agents = ['http:', 'https:'].flatMap(p => [jsonAgentFor(p), jsonAgentFor(p, 'probe'), downloadAgentFor(p)])

    for (const agent of agents) {
      expect((agent as any).options.timeout).toBe(CLIENT_KEEPALIVE_IDLE_MS)
      expect((agent as any).keepAlive).toBe(true)
    }
  })

  it("stays well under the backend's KEEP_ALIVE_IDLE_S (hermes_cli/web_server_transport.py)", () => {
    const py = readFileSync(join(__dirname, '../../../hermes_cli/web_server_transport.py'), 'utf8')
    const serverIdleS = Number(/^KEEP_ALIVE_IDLE_S = (\d+)/m.exec(py)?.[1])

    expect(serverIdleS).toBeGreaterThan(0)
    // Room for a slow tunnel: the client closes at least a minute before the server would.
    expect(serverIdleS * 1000 - CLIENT_KEEPALIVE_IDLE_MS).toBeGreaterThanOrEqual(60_000)
  })
})
