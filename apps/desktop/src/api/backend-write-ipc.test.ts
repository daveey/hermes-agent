// @vitest-environment node
import { EventEmitter } from 'node:events'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { runInNewContext } from 'node:vm'

import ts from 'typescript'
import { afterEach, expect, test, vi } from 'vitest'

import * as readiness from '../../../shared/src/backend-readiness-timeout'
import { coalescedBackendRead, ReadCoalescer } from '../../electron/read-coalescing'

import { HermesGateway } from './client'

const channel = 'hermes:backend:note-write'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

function compile(source: string): string {
  return ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText
}

function installBridge(coalescer: ReadCoalescer) {
  const bus = new EventEmitter()
  const main = ts.createSourceFile(
    'main.ts',
    readFileSync(join(__dirname, '../../electron', 'main.ts'), 'utf8'),
    ts.ScriptTarget.Latest,
    true
  )

  // Execute the actual top-level registration, not a substring (comments are not AST statements).
  // Booting all of main would launch windows/backends. Only its unrelated bootstrap is excluded here.
  const registrations = main.statements.filter(statement => {
    if (!ts.isExpressionStatement(statement) || !ts.isCallExpression(statement.expression)) {
      return false
    }
    const call = statement.expression

    return (
      ts.isPropertyAccessExpression(call.expression) &&
      call.expression.expression.getText(main) === 'ipcMain' &&
      call.expression.name.text === 'on' &&
      ts.isStringLiteral(call.arguments[0]) &&
      call.arguments[0].text === channel
    )
  })

  runInNewContext(compile(registrations.map(statement => statement.getText(main)).join('\n')), {
    ipcMain: bus,
    backendReadCoalescer: coalescer
  })

  let bridge: { noteBackendWrite?: () => void } | undefined

  const electron = {
    contextBridge: {
      exposeInMainWorld: (name: string, api: typeof bridge) => {
        if (name === 'hermesDesktop') {
          bridge = api
        }
      }
    },
    ipcRenderer: {
      sendSync: () => undefined,
      send: (name: string) => bus.emit(name, { sender: {} })
    },
    webFrame: {},
    webUtils: {}
  }

  // Evaluate the whole production preload so deleting/commenting its property or send cannot pass.
  runInNewContext(compile(readFileSync(join(__dirname, '../../electron', 'preload.ts'), 'utf8')), {
    exports: {},
    process: { platform: 'linux' },
    require: (name: string) => {
      if (name === 'electron') {
        return electron
      }

      if (name === './window-controls') {
        return { customWindowControlsEnabled: () => false }
      }

      if (name === '../../shared/src/backend-readiness-timeout') {
        return readiness
      }

      if (name === './api-expected-404') {
        return { unwrapExpectedNotFound: (value: unknown) => value }
      }
      throw new Error(`Unexpected preload dependency: ${name}`)
    }
  })
  vi.stubGlobal('window', { hermesDesktop: bridge })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

test.each(['success', 'rejected'] as const)(
  'real preload → registered main handler fences reads before and after a %s WS write',
  async outcome => {
    const coalescer = new ReadCoalescer()
    installBridge(coalescer)
    const wsPackage = 'ws'
    const { WebSocketServer } = await import(wsPackage)
    const server = new WebSocketServer({ host: '127.0.0.1', port: 0 })
    await new Promise<void>(resolve => server.once('listening', resolve))
    const received = deferred<void>()
    const answer = deferred<void>()
    server.on('connection', (socket: any) => {
      socket.on('message', async (bytes: Buffer) => {
        const frame = JSON.parse(bytes.toString())

        if (frame.method === 'session.mutate') {
          received.resolve()
          await answer.promise
        }

        const result =
          frame.method === 'session.resume'
            ? { session_id: 's', revision: 4, execution_generation: 2, running: false, messages: [], prompts: [] }
            : {
                session_id: 's',
                operation: frame.params?.operation,
                revision: 5,
                execution_generation: 2,
                title: 'New'
              }

        const response =
          frame.method === 'session.mutate' && outcome === 'rejected'
            ? { error: { code: 4001, message: 'session_busy' } }
            : { result }

        socket.send(JSON.stringify({ jsonrpc: '2.0', id: frame.id, ...response }))
      })
    })
    const client = new HermesGateway()
    const gates: ReturnType<typeof deferred<string>>[] = []

    const once = vi.fn(() => {
      const gate = deferred<string>()
      gates.push(gate)

      return gate.promise
    })

    const read = (baseUrl: string) => coalescedBackendRead(coalescer, { baseUrl }, '/api/sessions', {}, once)
    const origins = ['http://127.0.0.1:50101', 'http://127.0.0.1:50102']

    try {
      const address = server.address() as { port: number }
      await client.connect(`ws://127.0.0.1:${address.port}/api/ws?native_dial=fixture&ticket=one-use`)
      const before = origins.map(read)
      await client.request('session.resume', { session_id: 's' })
      const beforeJoin = origins.map(read)
      await Promise.resolve()
      expect(once).toHaveBeenCalledTimes(2) // non-writing RPC must not invalidate

      // Catch immediately: rejection is an expected outcome, not an unhandled promise.
      const writing = client.request('session.title', { session_id: 's', title: 'New' }).then(
        () => 'success',
        () => 'rejected'
      )

      await received.promise
      const during = origins.map(read)
      const duringJoin = origins.map(read)
      await Promise.resolve()
      expect(once).toHaveBeenCalledTimes(4) // send notice reached main before the wire request

      answer.resolve()
      expect(await writing).toBe(outcome)
      const after = origins.map(read)
      const afterJoin = origins.map(read)
      await Promise.resolve()
      expect(once).toHaveBeenCalledTimes(6) // settlement notice, even on rejection

      gates.forEach((gate, index) => gate.resolve(`answer ${index}`))
      expect(await Promise.all(before)).toEqual(['answer 0', 'answer 1'])
      expect(await Promise.all(beforeJoin)).toEqual(['answer 0', 'answer 1'])
      expect(await Promise.all(during)).toEqual(['answer 2', 'answer 3'])
      expect(await Promise.all(duringJoin)).toEqual(['answer 2', 'answer 3'])
      expect(await Promise.all(after)).toEqual(['answer 4', 'answer 5'])
      expect(await Promise.all(afterJoin)).toEqual(['answer 4', 'answer 5'])
    } finally {
      answer.resolve()
      gates.forEach(gate => gate.resolve('cleanup'))
      client.close()

      for (const socket of server.clients) {
        socket.terminate()
      }
      await new Promise<void>(resolve => server.close(() => resolve()))
    }
  }
)
