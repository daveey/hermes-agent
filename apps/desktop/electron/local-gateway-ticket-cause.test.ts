/**
 * A ticket mint that fails names its cause (`code` + message suffix) and retries one transient
 * connect failure: under Mac load the control socket refuses (full backlog) or misses the deadline,
 * and a bare 'Gateway ticket bootstrap failed' hid which one it was.
 */
import fs from 'node:fs/promises'
import net from 'node:net'
import path from 'node:path'

import { afterEach, beforeEach, expect, test } from 'vitest'

import { gatewayTicketTiming, isStaleLocalGatewayError, mintLocalGatewayTicket } from './local-gateway'
import { shortSocketTmpDir } from './local-gateway.test-helpers'

const posix = process.platform !== 'win32'
const saved = { ...gatewayTicketTiming }
let home = ''
let servers: net.Server[] = []

const endpoint = () => ({
  profile_id: home,
  instance_id: 'owner',
  authority_epoch: 1,
  runtime_protocol: 1,
  api_origin: 'http://127.0.0.1:1234',
  capabilities: ['session-authority-v1'],
  supervisor: 'none'
})
const socketPath = () => path.join(home, 'gateway.sock')
const grant = (ticket: string) =>
  JSON.stringify({ protocol: 1, id: 1, ok: true, result: { profile_id: home, instance_id: 'owner', ticket } }) + '\n'

async function listen(at: string, onRequest: (socket: net.Socket, n: number) => void) {
  let n = 0
  const server = net.createServer(socket => socket.once('data', () => onRequest(socket, ++n)))
  servers.push(server)
  await new Promise<void>(resolve => server.listen(at, resolve))
  await fs.chmod(at, 0o600)

  return { connections: () => n, server }
}

/** A socket inode with no listener behind it: connect() answers ECONNREFUSED. */
async function deadSocket() {
  const scratch = path.join(home, 'dead.sock')
  const server = net.createServer()
  await new Promise<void>(resolve => server.listen(scratch, resolve))
  await fs.rename(scratch, socketPath())
  await new Promise<void>(resolve => server.close(() => resolve()))
  await fs.chmod(socketPath(), 0o600)
}

beforeEach(async () => {
  home = await shortSocketTmpDir('desktop-cause-')
  await fs.chmod(home, 0o700)
  Object.assign(gatewayTicketTiming, { deadlineMs: 150, retryDelayMs: 200 })
})

afterEach(async () => {
  Object.assign(gatewayTicketTiming, saved)
  await Promise.all(servers.map(s => new Promise<void>(resolve => s.close(() => resolve()))))
  servers = []
  await fs.rm(home, { recursive: true, force: true })
})

test.skipIf(!posix)('a stalled listener surfaces code "deadline", after one retry', async () => {
  const silent = await listen(socketPath(), () => {})
  const failure = await mintLocalGatewayTicket(endpoint()).catch(error => error)
  expect(failure.code).toBe('deadline')
  expect(failure.message).toBe('Gateway ticket bootstrap failed (deadline 150ms)')
  expect(isStaleLocalGatewayError(failure)).toBe(true)
  expect(silent.connections()).toBe(2)
})

test.skipIf(!posix)('a refused connect surfaces code ECONNREFUSED, after one retry', async () => {
  await deadSocket()
  const started = Date.now()
  const failure = await mintLocalGatewayTicket(endpoint()).catch(error => error)
  expect(failure.code).toBe('ECONNREFUSED')
  expect(failure.message).toBe('Gateway ticket bootstrap failed (ECONNREFUSED)')
  // The message prefix callers (redial, session-mentions PRE_SEND_RE) match on is kept.
  expect(failure.message.startsWith('Gateway ticket bootstrap failed')).toBe(true)
  expect(Date.now() - started).toBeGreaterThanOrEqual(180)
})

test.skipIf(!posix)('one refused connect followed by a live listener returns the ticket', async () => {
  await deadSocket()
  const minting = mintLocalGatewayTicket(endpoint())
  // The first dial is refused at once; the owner republishes the socket inside the retry delay.
  await new Promise(resolve => setTimeout(resolve, 60))
  await fs.rm(socketPath())
  const live = await listen(socketPath(), socket => socket.end(grant('after-refusal')))
  expect(await minting).toBe('after-refusal')
  expect(live.connections()).toBe(1)
})

test.skipIf(!posix)('one missed deadline followed by an answer returns the ticket', async () => {
  const flaky = await listen(socketPath(), (socket, n) => {
    if (n > 1) {
      socket.end(grant('second-dial'))
    }
  })
  expect(await mintLocalGatewayTicket(endpoint())).toBe('second-dial')
  expect(flaky.connections()).toBe(2)
})

test.skipIf(!posix)('a missing socket is not retried', async () => {
  const started = Date.now()
  await expect(mintLocalGatewayTicket(endpoint())).rejects.toThrow('Gateway ticket control socket missing')
  expect(Date.now() - started).toBeLessThan(150)
})

test.skipIf(!posix)('a socket unlinked after a refused dial reports missing, with no second dial', async () => {
  await deadSocket()
  const minting = mintLocalGatewayTicket(endpoint()).catch(error => error)
  await new Promise(resolve => setTimeout(resolve, 60))
  await fs.rm(socketPath())
  const failure = await minting
  expect(failure.message).toBe('Gateway ticket control socket missing')
  expect(failure.code).toBeUndefined()
})
