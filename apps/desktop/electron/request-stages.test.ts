import fs from 'node:fs/promises'
import http from 'node:http'
import type { AddressInfo } from 'node:net'
import os from 'node:os'
import path from 'node:path'

import ts from 'typescript'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { destroyKeepaliveAgents, keepAliveAgent, sendWithRequestDeadline, startRequestDeadline } from './api-transport'
import { fetchJsonWith, fetchPublicJsonWith } from './json-fetch'
import * as tracing from './request-stages'

const deps = { remoteHeaders: () => ({}), nativeHeaders: async () => null }
const route = '/api/plugins/user-steps/requests'

function capture() {
  const rows: any[] = []
  const buffer = new tracing.StageBuffer(async lines => {
    rows.push(
      ...lines
        .trim()
        .split('\n')
        .map(line => JSON.parse(line))
    )
  })

  return { rows, buffer }
}

async function serverFor(handler: http.RequestListener) {
  const server = http.createServer(handler)
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`

  return {
    base,
    close: async () => {
      server.closeAllConnections()
      await new Promise<void>(resolve => server.close(() => resolve()))
    }
  }
}

afterEach(() => {
  vi.restoreAllMocks()
  destroyKeepaliveAgents()
})

describe('request-stage capture', () => {
  it.each([route, '/api/plugins/session-mentions/result'])(
    'executes the actual main dispatcher wrapper for %s with trace and error identity intact',
    async selectedPath => {
      const source = await fs.readFile(new URL('./main.ts', import.meta.url), 'utf8')
      const ast = ts.createSourceFile('main.ts', source, ts.ScriptTarget.Latest, true)
      const declaration = ast.statements.find(
        statement => ts.isFunctionDeclaration(statement) && statement.name?.text === 'handleHermesApiRequest'
      )!
      const { rows, buffer } = capture()
      const failure = new Error('private')

      const operation = vi.fn(async request => {
        if (request.fail) {
          throw failure
        }

        return 42
      })

      // Execute production declaration while isolating only Electron's heavy bootstrap.
      const handle = new Function(
        'withApiRequestStages',
        'handleHermesApiRequestUntraced',
        declaration.getText(ast) + '; return handleHermesApiRequest'
      )((request, run) => tracing.withApiRequestStages(request, run, buffer), operation)

      expect(await handle({ path: selectedPath })).toBe(42)
      await expect(handle({ path: selectedPath, fail: true })).rejects.toBe(failure)
      await buffer.flush()
      expect(operation).toHaveBeenCalledTimes(2)
      expect(rows.map(row => row.stage)).toEqual(['api_call', 'api_end', 'api_call', 'api_error'])
    }
  )

  it('persists and rotates four bounded files using asynchronous IO', async () => {
    const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'request-stages-'))
    const file = path.join(directory, 'capture.jsonl')

    try {
      const write = tracing.stageFileWriter(file, 60)

      for (let i = 0; i < 40; i++) {
        await write(JSON.stringify({ seq: i }) + '\n')
      }
      expect((await fs.readdir(directory)).sort()).toEqual([
        'capture.jsonl',
        'capture.jsonl.1',
        'capture.jsonl.2',
        'capture.jsonl.3'
      ])

      for (const name of await fs.readdir(directory)) {
        expect((await fs.stat(path.join(directory, name))).size).toBeLessThanOrEqual(60)
      }
      expect(
        (await fs.readFile(file, 'utf8'))
          .trim()
          .split('\n')
          .map(line => JSON.parse(line))
          .at(-1).seq
      ).toBe(39)
      // Reinitializing discovers the existing length rather than silently exceeding the cap.
      const resumed = tracing.stageFileWriter(file, 60)
      await resumed('{"seq":40}\n')
      expect((await fs.stat(file)).size).toBeLessThanOrEqual(60)
      const buffer = new tracing.StageBuffer(tracing.stageFileWriter(path.join(file, 'invalid')))
      buffer.emit('lost')
      await buffer.flush()
      expect(buffer.ioLost).toBe(1)
    } finally {
      await fs.rm(directory, { recursive: true, force: true })
    }
  })

  it.each([route, '/api/plugins/session-mentions/result'])(
    'records real JSON helper stages for %s without private data',
    async selectedPath => {
      const { rows, buffer } = capture()
      const real = tracing.requestStages
      vi.spyOn(tracing, 'requestStages').mockImplementation((url, method) => real(url, method, buffer))
      const ids: string[] = []

      const server = await serverFor((req, res) => {
        ids.push(String(req.headers['x-hermes-req-id']))
        expect(req.method).toBe('POST')
        req.resume()
        res.end('{"ok":true}')
      })

      try {
        for (const fetch of [
          fetchJsonWith,
          (_deps: typeof deps, url: string, _token: string | null, options: { method: string; body: unknown }) =>
            fetchPublicJsonWith(_deps, url, options)
        ]) {
          const answer = await tracing.withApiRequestStages(
            { path: selectedPath + '?token=DO_NOT_LOG', method: 'POST' },
            async () => {
              // Yield before fetch, as a real asynchronous dial/gate does.
              await Promise.resolve()

              return fetch(deps, server.base + selectedPath + '?token=DO_NOT_LOG', null, {
                method: 'POST',
                body: { private: 'DO_NOT_LOG' }
              })
            },
            buffer
          )

          expect(answer).toEqual({ ok: true })
        }

        await buffer.flush()
        expect(ids).toHaveLength(2)
        expect(new Set(ids).size).toBe(2)

        for (const id of ids) {
          expect(id).toMatch(/^[a-f0-9]{32}$/)
          const events = rows.filter(row => row.id === id)
          const names = events.map(row => row.stage)

          for (const stage of [
            'call',
            'agent_queue',
            'socket_assigned',
            'connected',
            'write_finished',
            'response_start',
            'response_end'
          ]) {
            expect(names).toContain(stage)
          }

          expect(names.indexOf('socket_assigned')).toBeLessThan(names.indexOf('write_finished'))
          expect(events.find(row => row.stage === 'response_end').bytes).toBe(11)
          const call = rows.find(row => row.stage === 'api_call' && row.call_id === events[0].call_id)
          expect(call.mono_ms).toBeLessThanOrEqual(events[0].mono_ms)
        }

        expect(JSON.stringify(rows)).not.toContain('DO_NOT_LOG')
      } finally {
        await server.close()
      }
    }
  )

  it.each(['private', 'public'])('records %s response receipt before synchronous decoding', async kind => {
    const observed: string[] = []
    const { rows, buffer } = capture()
    const emit = buffer.emit.bind(buffer)
    vi.spyOn(buffer, 'emit').mockImplementation((stage, fields) => {
      observed.push(stage)
      emit(stage, fields)
    })
    const realTrace = tracing.requestStages
    vi.spyOn(tracing, 'requestStages').mockImplementation((url, method) => realTrace(url, method, buffer))
    const parse = JSON.parse
    vi.spyOn(JSON, 'parse').mockImplementation((text, reviver) => {
      if (text === '{"ok":true}') {
        observed.push('decode_started')
      }

      return parse(text, reviver)
    })
    const server = await serverFor((_req, res) => res.end('{"ok":true}'))

    try {
      const url = server.base + route
      const answer = kind === 'private' ? await fetchJsonWith(deps, url, null) : await fetchPublicJsonWith(deps, url)
      expect(answer).toEqual({ ok: true })
      expect(observed).toContain('response_end')
      expect(observed).toContain('decode_started')
      expect(observed.indexOf('response_end')).toBeLessThan(observed.indexOf('decode_started'))
      await buffer.flush()
      expect(rows.filter(row => row.stage === 'response_end')).toHaveLength(1)
      expect(rows.find(row => row.stage === 'response_end').bytes).toBe(11)
    } finally {
      await server.close()
      await buffer.flush()
    }
  })

  it('records Agent queue deadline before a socket and does not change POST replay policy', async () => {
    const { rows, buffer } = capture()
    const agent = keepAliveAgent(http, 1)
    let arrived!: () => void
    const firstArrived = new Promise<void>(resolve => {
      arrived = resolve
    })
    let hits = 0
    const server = await serverFor(() => {
      hits++
      arrived()
    })
    const first = http.get(server.base + route, { agent })
    first.on('error', () => {})
    await firstArrived

    try {
      const trace = tracing.requestStages(server.base + route, 'POST', buffer)!
      const deadline = startRequestDeadline(50, Date.now, trace.deadline)

      const error = await new Promise<Error>(resolve => {
        sendWithRequestDeadline(deadline, () => http.request(server.base + route, { agent, method: 'POST' }), {
          onError: resolve,
          observeRequest: trace.attach
        })
      })

      expect(error.message).toContain('Timed out connecting')
      await buffer.flush()
      const names = rows.filter(row => row.id === trace.id).map(row => row.stage)
      expect(names).toContain('agent_queue')
      expect(names).toContain('deadline')
      expect(names).not.toContain('socket_assigned')
      expect(hits).toBe(1)
    } finally {
      first.destroy()
      agent.destroy()
      await server.close()
    }
  })

  it('does not duplicate an ambiguous mutation and assigns retries their own ids', async () => {
    const { rows, buffer } = capture()
    const real = tracing.requestStages
    vi.spyOn(tracing, 'requestStages').mockImplementation((url, method) => real(url, method, buffer))
    const ids: string[] = []

    const server = await serverFor((req, res) => {
      ids.push(String(req.headers['x-hermes-req-id']))
      req.resume()
      req.on('end', () => {
        if (req.method === 'POST' || ids.length === 2) {
          req.socket.destroy()
        } else {
          res.end('{}')
        }
      })
    })

    try {
      await expect(
        fetchJsonWith(deps, server.base + route, null, { method: 'POST', body: { private: 'DO_NOT_LOG' } })
      ).rejects.toThrow()
      expect(ids).toHaveLength(1)
      expect(await fetchJsonWith(deps, server.base + route, null)).toEqual({})
      expect(ids).toHaveLength(3)
      expect(new Set(ids).size).toBe(3)
      await buffer.flush()
      expect(JSON.stringify(rows)).not.toContain('DO_NOT_LOG')
    } finally {
      await server.close()
    }
  })

  it('exposes overload and IO gaps while a writer is blocked, then recovers', async () => {
    let release!: () => void
    const blocked = new Promise<void>(resolve => {
      release = resolve
    })
    const rows: any[] = []
    let writes = 0

    const buffer = new tracing.StageBuffer(async lines => {
      if (++writes === 1) {
        await blocked
        throw new Error('private')
      }
      rows.push(
        ...lines
          .trim()
          .split('\n')
          .map(line => JSON.parse(line))
      )
    }, 2)

    buffer.emit('first')
    const pending = buffer.flush()
    buffer.emit('second')
    buffer.emit('third')
    buffer.emit('drop')
    expect(buffer.dropped).toBe(1)
    release()
    await pending
    buffer.emit('coverage')
    await buffer.flush()
    expect(rows.at(-1)).toMatchObject({ stage: 'coverage', dropped: 1, io_lost: 1, seq: 5 })
    expect(JSON.stringify(rows)).not.toContain('private')
  })

  it('skips private paths and preserves outer gate errors', async () => {
    const { rows, buffer } = capture()
    expect(tracing.requestStages('http://localhost/private?secret=x', 'GET', buffer)).toBeUndefined()
    expect(tracing.requestStages('http://[invalid', 'GET', buffer)).toBeUndefined()
    expect(await tracing.withApiRequestStages({ path: 'http://[invalid' }, async () => 7, buffer)).toBe(7)
    expect(await tracing.withApiRequestStages({ path: '/private' }, async () => 2, buffer)).toBe(2)
    const error = new Error('private')
    await expect(
      tracing.withApiRequestStages(
        { path: route },
        async () => {
          throw error
        },
        buffer
      )
    ).rejects.toBe(error)
    await buffer.flush()
    expect(rows.map(row => row.stage)).toEqual(['api_call', 'api_error'])
  })
})
