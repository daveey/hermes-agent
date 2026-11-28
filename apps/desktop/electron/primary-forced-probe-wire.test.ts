import { execFileSync, spawn } from 'node:child_process'
import crypto from 'node:crypto'
import { once } from 'node:events'
import fs from 'node:fs'
import http from 'node:http'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

import { expect, test } from 'vitest'
import { WebSocket, WebSocketServer } from 'ws'

import { destroyKeepaliveAgents } from './api-transport'
import { fetchJsonWith } from './json-fetch'
import { primaryRevalidationMainFixture } from './primary-revalidation-main.fixture'
import { dispatchThroughRemoteDownGate, RemoteDownGate } from './remote-down-gate'
import { pickLocalPort, SshConnection } from './ssh-connection'

const wireTest = test.skipIf(process.platform !== 'linux' || !fs.existsSync('/usr/sbin/sshd'))
const deps = { remoteHeaders: () => ({}), nativeHeaders: async () => null }
const read = (url: string, opts: any = {}) => fetchJsonWith(deps, url, 'fixture', opts)

async function fixture() {
  const dir = fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), 'p-'))
  expect(Buffer.byteLength(path.join(dir, '0'.repeat(16) + '.sock.' + 'X'.repeat(16)))).toBeLessThan(108)
  const sockets = new Set<net.Socket>()
  const connections: SshConnection[] = []
  const webSockets: WebSocket[] = []
  const servers: net.Server[] = []
  const timers = new Set<ReturnType<typeof setTimeout>>()
  const log: string[] = []
  let slow = false
  let queued = 0
  let mutations = 0

  let slowStarted: () => void = () => {}

  const slowSeen = new Promise<void>(r => {
    slowStarted = r
  })

  const held: http.ServerResponse[] = []
  let sshd: ReturnType<typeof spawn> | undefined

  const watch = (s: net.Socket) => {
    sockets.add(s)
    s.on('error', () => {})
    s.on('close', () => sockets.delete(s))
  }

  const listen = async (server: net.Server) => {
    servers.push(server)
    server.on('connection', watch)
    server.listen(0, '127.0.0.1')
    await once(server, 'listening')

    return (server.address() as net.AddressInfo).port
  }

  const close = async () => {
    slow = false
    destroyKeepaliveAgents()

    for (const ws of webSockets) {
      ws.terminate()
    }

    for (const c of connections) {
      await c.close()
    }

    for (const timer of timers) {
      clearTimeout(timer)
    }

    for (const s of sockets) {
      s.destroy()
    }

    await Promise.all(servers.map(s => new Promise<void>(r => s.close(() => r()))))

    if (sshd && sshd.exitCode === null) {
      sshd.kill()
      await once(sshd, 'exit')
    }

    fs.rmSync(dir, { recursive: true, force: true })
  }

  try {
    execFileSync('ssh-keygen', ['-q', '-t', 'ed25519', '-N', '', '-f', dir + '/key'])
    fs.copyFileSync(dir + '/key.pub', dir + '/authorized')
    const sshPort = Number(await pickLocalPort())
    fs.writeFileSync(
      dir + '/sshd_config',
      [
        `Port ${sshPort}`,
        'ListenAddress 127.0.0.1',
        `HostKey ${dir}/key`,
        `PidFile ${dir}/sshd.pid`,
        `AuthorizedKeysFile ${dir}/authorized`,
        'StrictModes no',
        'UsePAM no',
        'PasswordAuthentication no',
        'KbdInteractiveAuthentication no',
        'PermitUserRC no',
        'AllowTcpForwarding local',
        'LogLevel VERBOSE'
      ].join('\n')
    )
    sshd = spawn('/usr/sbin/sshd', ['-D', '-e', '-f', dir + '/sshd_config'], { stdio: ['ignore', 'ignore', 'pipe'] })
    await new Promise<void>((resolve, reject) => {
      let stderr = ''
      const timer = setTimeout(() => reject(Error('sshd readiness: ' + stderr)), 3000)
      timers.add(timer)
      sshd!.stderr!.on('data', chunk => {
        stderr += chunk
        log.push(String(chunk))

        if (stderr.includes('Server listening')) {
          clearTimeout(timer)
          timers.delete(timer)
          resolve()
        }
      })
      sshd!.once('exit', code => {
        clearTimeout(timer)
        timers.delete(timer)
        reject(Error('sshd exit ' + code + ': ' + stderr))
      })
    })

    // Rate limit only this fixture's encrypted server->client stream. There is
    // no packet loss injection, user-device access or production process signal.
    const relayPort = await listen(
      net.createServer(client => {
        const up = net.connect(sshPort, '127.0.0.1')
        watch(up)
        client.pipe(up)
        const queue: Buffer[] = []
        let bytes = 0
        let running = false

        const pump = () => {
          if (running || queue.length === 0 || client.destroyed) {
            return
          }

          running = true

          const timer = setTimeout(
            () => {
              timers.delete(timer)
              running = false

              if (client.destroyed || queue.length === 0) {
                return
              }

              const b = queue.shift()!
              bytes -= b.length
              queued -= b.length
              client.write(b)

              if (bytes < 262144) {
                up.resume()
              }

              pump()
            },
            slow ? 80 : 0
          )

          timers.add(timer)
        }

        up.on('data', b => {
          if (!slow && queue.length === 0 && !running) {
            client.write(b)

            return
          }

          for (let i = 0; i < b.length; i += 8192) {
            const part = b.subarray(i, i + 8192)
            queue.push(part)
            bytes += part.length
            queued += part.length
          }

          if (bytes > 1048576) {
            up.pause()
          }

          pump()
        })
        client.on('close', () => {
          up.destroy()
          queued -= bytes
          bytes = 0
          queue.length = 0
        })
        up.on('error', () => client.destroy())
        up.on('end', () => client.end())
      })
    )

    const body = Buffer.from(JSON.stringify({ data: crypto.randomBytes(2 * 1024 * 1024).toString('hex') }))

    const backend = http.createServer((req, res) => {
      req.resume()
      req.on('end', () => {
        if (req.method === 'POST') {
          mutations++
          req.socket.destroy()

          return
        }

        if (req.url === '/api/plugins/slow/status') {
          held.push(res)
          slowStarted()

          return
        }

        res.setHeader('Content-Type', 'application/json')
        res.end(req.url?.includes('/fleet') ? body : '{"ok":true}')
      })
    })

    const ws = new WebSocketServer({ server: backend })
    ws.on('connection', socket => socket.send('wire-ready'))
    const backendPort = await listen(backend)

    const dial = async (attempt: number) => {
      const ssh = new SshConnection(
        {
          host: '127.0.0.1',
          user: os.userInfo().username,
          port: attempt === 0 ? relayPort : sshPort,
          keyPath: dir + '/key'
        },
        {
          controlDir: dir,
          connectTimeoutMs: 3000,
          forwardTimeoutMs: 3000,
          spawnFn: (command, args, options) =>
            spawn(
              command,
              [
                '-F',
                '/dev/null',
                '-o',
                `UserKnownHostsFile=${dir}/known_hosts`,
                '-o',
                'StrictHostKeyChecking=no',
                ...args
              ],
              options
            )
        }
      )

      connections.push(ssh)
      await ssh.open()
      const port = Number(await pickLocalPort())
      await ssh.forward(port, backendPort)

      return { ssh, port, remotePort: backendPort }
    }

    const api = primaryRevalidationMainFixture({ dial, read, log: s => log.push(s) })
    let gateNow = 0
    const gate = new RemoteDownGate(() => gateNow)

    const get = (url: string, options = {}) =>
      dispatchThroughRemoteDownGate({
        gate,
        key: 'hub',
        connect: () => api.ensure('hub', 'default'),
        request: (c: any) => read(c.baseUrl + url, { timeoutMs: 4000, ...options })
      })

    const wsRead = async () => {
      const socket = new WebSocket(await api.wsUrl('served'), { handshakeTimeout: 2000 })
      webSockets.push(socket)
      socket.on('error', () => {})

      return String((await once(socket, 'message'))[0])
    }

    return {
      api,
      get,
      wsRead,
      connections,
      gate,
      close,
      slowSeen,
      advanceGate: () => {
        gateNow += 2000
      },
      mutations: () => mutations,
      shape: () => {
        slow = true
      },
      bulk: async () => {
        const pending = get('/api/plugins/runners/v1/fleet').catch(error => error)
        const end = Date.now() + 3000

        while (queued < 500000) {
          if (Date.now() > end) {
            throw Error('bulk did not reach encrypted return queue')
          }

          await delay(10)
        }

        return { pending }
      }
    }
  } catch (error) {
    await close()
    throw error
  }
}

wireTest(
  'forced registry check retires the real shared-primary master and fresh HTTP/WS recover without mutation replay',
  async () => {
    const f = await fixture()

    try {
      expect(await f.get('/ready')).toEqual({ ok: true })
      await f.api.prewarmer.start()
      expect(await f.wsRead()).toBe('wire-ready')
      const old = f.connections[0]
      const oldPath = old.controlPath
      await expect(f.get('/mutation', { method: 'POST', body: { value: 1 } })).rejects.toThrow()
      expect(f.mutations()).toBe(1)
      expect(f.gate.isDown('hub')).toBe(true)
      f.advanceGate() // Only gate backoff is injected; socket/deadline clocks remain real.
      expect(await f.get('/ready')).toEqual({ ok: true })
      f.shape()
      const bulk = await f.bulk()
      await expect(f.get('/api/plugins/hub-sync/status')).rejects.toThrow('Timed out')
      expect(await old.isAlive()).toBe(true)

      for (let i = 0; i < 3; i++) {
        await Promise.all([
          f.api.prewarmer.tick(),
          ...Array.from({ length: 7 }, () => f.api.ensure('hub', 'default', '', { forceProbe: true }))
        ])
      }

      expect(await old.isAlive()).toBe(false)
      expect(fs.existsSync(oldPath)).toBe(false)
      expect(f.api.stats()).toMatchObject({ dials: 2, probes: 3, probePools: ['probe', 'probe', 'probe'] })
      await bulk.pending
      f.advanceGate()
      expect(await f.get('/later')).toEqual({ ok: true })
      expect(await f.wsRead()).toBe('wire-ready')
      expect(f.mutations()).toBe(1)
      expect(f.gate.isDown('hub')).toBe(false)
    } finally {
      f.api.prewarmer.stop()
      await f.close()
    }
  },
  20_000
)

wireTest(
  'slow endpoint alone never replaces a healthy shared-primary transport during forced checks',
  async () => {
    const f = await fixture()

    try {
      expect(await f.get('/ready')).toEqual({ ok: true })
      await f.api.prewarmer.start()
      const old = f.connections[0]
      const slow = f.get('/api/plugins/slow/status', { timeoutMs: 300 }).catch(error => error)
      await f.slowSeen

      for (let i = 0; i < 4; i++) {
        await f.api.prewarmer.tick()
      }

      expect(await old.isAlive()).toBe(true)
      expect(f.api.stats()).toMatchObject({ dials: 1, probes: 4, probePools: ['probe', 'probe', 'probe', 'probe'] })
      expect(await f.get('/later')).toEqual({ ok: true })
      expect(await f.wsRead()).toBe('wire-ready')
      expect(await slow).toBeInstanceOf(Error)
    } finally {
      f.api.prewarmer.stop()
      await f.close()
    }
  },
  10_000
)
