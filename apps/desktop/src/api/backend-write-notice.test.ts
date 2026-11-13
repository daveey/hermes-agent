// @vitest-environment node
import { afterEach, expect, test, vi } from 'vitest'

import { isBackendWriteMethod } from './canonical-protocol'
import { HermesGateway } from './client'

// Main's read coalescer (electron/read-coalescing.ts) shares an identical in-flight REST GET. A session
// mutation that travels over the gateway WebSocket never passes through that REST sink, so without a notice
// a list refetch right after `session.title` could join a list GET sent before the rename and show the old
// title. Every WS-RPC write tells main before it leaves and again once it has settled.
afterEach(() => {
  vi.unstubAllGlobals()
})

test('session mutations, policy edits and session.create are writes; reads, turns and controls are not', () => {
  const writes = [
    'session.title',
    'session.archive',
    'session.compress',
    'session.rewind',
    'session.model',
    'session.branch',
    'session.branch_stored',
    'session.branch_whole',
    'slash.exec',
    'config.set',
    'session.workspace.move',
    'session.cwd.set',
    'session.create'
  ]

  const others = [
    'session.resume',
    'session.activate',
    'session.list',
    'session.usage',
    'session.control.read',
    'prompt.submit',
    'session.interrupt',
    'config.get',
    'client.capabilities'
  ]

  expect(writes.filter(method => !isBackendWriteMethod(method))).toEqual([])
  expect(others.filter(isBackendWriteMethod)).toEqual([])
})

async function fixtureGateway(answer: (frame: any) => { result?: unknown; error?: unknown }) {
  const wsPackage = 'ws'
  const { WebSocketServer } = await import(wsPackage)
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 })
  await new Promise<void>(resolve => server.once('listening', resolve))
  const log: string[] = []
  server.on('connection', (socket: any) => {
    socket.on('message', (bytes: Buffer) => {
      const frame = JSON.parse(bytes.toString())

      // The dial's own capability handshake is not part of what these tests order.
      if (!frame.method.startsWith('client.')) {
        log.push(`wire ${frame.method}`)
      }

      socket.send(JSON.stringify({ jsonrpc: '2.0', id: frame.id, ...answer(frame) }))
    })
  })
  vi.stubGlobal('window', { hermesDesktop: { noteBackendWrite: () => log.push('notice') } })
  const client = new HermesGateway()
  const address = server.address() as { port: number }
  await client.connect(`ws://127.0.0.1:${address.port}/api/ws?native_dial=fixture&ticket=one-use`)

  const close = async () => {
    client.close()

    for (const socket of server.clients) {
      socket.terminate()
    }

    await new Promise<void>(resolve => server.close(() => resolve()))
  }

  return { client, close, log }
}

const resume = { session_id: 's', revision: 4, execution_generation: 2, running: false, messages: [], prompts: [] }

test('a WS-RPC session mutation notifies main before it is sent and after it settles; reads do not', async () => {
  const { client, close, log } = await fixtureGateway(frame =>
    frame.method === 'session.resume'
      ? { result: resume }
      : {
          result: {
            session_id: 's',
            operation: frame.params.operation,
            revision: 5,
            execution_generation: 2,
            title: 'New'
          }
        }
  )

  try {
    await client.request('session.resume', { session_id: 's' })
    expect(log).toEqual(['wire session.resume'])

    await client.request('session.title', { session_id: 's', title: 'New' })
    await client.request('session.archive', { session_id: 's', archived: true })
    expect(log).toEqual([
      'wire session.resume',
      'notice',
      'wire session.mutate',
      'notice',
      'notice',
      'wire session.mutate',
      'notice'
    ])
  } finally {
    await close()
  }
})

test('a refused mutation still notifies main once it has settled', async () => {
  const { client, close, log } = await fixtureGateway(frame =>
    frame.method === 'session.resume'
      ? { result: resume }
      : { error: { code: 4001, message: 'session_busy', data: { reason: 'session_busy' } } }
  )

  try {
    await client.request('session.resume', { session_id: 's' })
    await expect(client.request('session.title', { session_id: 's', title: 'New' })).rejects.toThrow()
    expect(log).toEqual(['wire session.resume', 'notice', 'wire session.mutate', 'notice'])
  } finally {
    await close()
  }
})
