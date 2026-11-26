import { AsyncLocalStorage } from 'node:async_hooks'
// Local-only, bounded transport evidence. No URL queries, headers, bodies or error messages.
import { randomUUID } from 'node:crypto'
import fs from 'node:fs/promises'
import type { ClientRequest } from 'node:http'
import os from 'node:os'
import path from 'node:path'

import { resolveDesktopHermesHome } from './data-paths'

const POLL_PATHS = new Set([
  '/api/plugins/user-steps/requests', '/api/plugins/hub-sync/status',
  '/api/plugins/kanban/board', '/api/sessions', '/api/health',
  '/api/plugins/session-mentions/result'
])

type Row = Record<string, string | number | boolean>

/** Bounded admission while one async batch is writing. Never wait for IO in an HTTP callback. */
export class StageBuffer {
  private rows: Row[] = []
  private writing = false
  private scheduled = false
  private seq = 0
  dropped = 0
  ioLost = 0
  readonly boot = randomUUID().replaceAll('-', '')

  constructor(private write: (lines: string) => Promise<void>, private capacity = 4096) {}

  emit(stage: string, fields: Row = {}) {
    const seq = ++this.seq

    if (this.rows.length >= this.capacity) {
      this.dropped++

      return
    }

    this.rows.push({ ...fields, stage, seq, boot: this.boot, pid: process.pid,
      utc_ms: Date.now(), mono_ms: performance.now(), dropped: this.dropped, io_lost: this.ioLost })

    if (!this.scheduled && !this.writing) {
      this.scheduled = true
      setImmediate(() => { this.scheduled = false; void this.flush() })
    }
  }

  async flush() {
    if (this.writing) {
      return
    }

    this.writing = true

    try {
      while (this.rows.length) {
        const batch = this.rows.splice(0, 128)

        try {
          await this.write(batch.map(row => JSON.stringify(row)).join('\n') + '\n')
        } catch {
          // No raw exception text: paths and credentials may be in an IO error.
          this.ioLost += batch.length
        }
      }
    } finally {
      this.writing = false
    }
  }
}

let sink: StageBuffer | undefined
const calls = new AsyncLocalStorage<string>()

function pollUrl(raw: string, base?: string): URL | undefined {
  try {
    const parsed = new URL(raw, base)

    return POLL_PATHS.has(parsed.pathname) ? parsed : undefined
  } catch {
    // Leave malformed-input errors to the existing request validation boundary.
    return undefined
  }
}

/** Covers registry/gate/dial/coalescing waits before a request has an HTTP Agent. */
export async function withApiRequestStages<T>(request: { path?: string; method?: string }, operation: () => Promise<T>, output?: StageBuffer): Promise<T> {
  const parsed = pollUrl(request.path || '/', 'http://localhost')

  if (!parsed) { return operation() }
  const pathname = parsed.pathname
  const callId = randomUUID().replaceAll('-', '')
  const target = output || journal()
  const fields = { call_id: callId, path: pathname }
  target.emit('api_call', fields)

  return calls.run(callId, async () => {
    try {
      const result = await operation()
      target.emit('api_end', fields)

      return result
    } catch (error) {
      target.emit('api_error', fields)
      throw error
    }
  })
}

export function stageFileWriter(file: string, maxBytes = 16 * 1024 * 1024) {
    let size = 0
    let ready = false

    return async (lines: string) => {
      if (!ready) {
        await fs.mkdir(path.dirname(file), { recursive: true })

        try { size = (await fs.stat(file)).size } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== 'ENOENT') { throw error }
        }

        ready = true
      }

      if (size + Buffer.byteLength(lines) > maxBytes) {
        for (let i = 2; i >= 0; i--) {
          try { await fs.rename(i ? `${file}.${i}` : file, `${file}.${i + 1}`) } catch (error) {
            if ((error as NodeJS.ErrnoException).code !== 'ENOENT') { throw error }
          }
        }

        size = 0
      }

      await fs.appendFile(file, lines, { mode: 0o600 })
      size += Buffer.byteLength(lines)
    }
}

function journal(): StageBuffer {
  if (!sink) {
    const file = path.join(resolveDesktopHermesHome({ home: os.homedir() }), 'logs', `request-stages-desktop-${process.pid}.jsonl`)
    sink = new StageBuffer(stageFileWriter(file))
    const timer = setInterval(() => sink?.emit('coverage'), 1000)
    timer.unref()
  }

  return sink
}

export function requestStages(url: string, method: string, output?: StageBuffer) {
  const parsed = pollUrl(url)

  if (!parsed) {
    return undefined
  }

  const id = randomUUID().replaceAll('-', '')

  const fields = { id, call_id: calls.getStore() || '', path: parsed.pathname,
    method: /^(GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS)$/.test(method) ? method : 'OTHER',
    port: Number(parsed.port || (parsed.protocol === 'https:' ? 443 : 80)) }

  const target = output || journal()
  const emit = (stage: string, extras: Row = {}) => target.emit(stage, { ...fields, ...extras })
  emit('call')

  return {
    id,
    deadline: () => emit('deadline'),
    attach(req: ClientRequest) {
      emit('agent_queue')
      req.once('socket', socket => {
        emit('socket_assigned', { reused: req.reusedSocket })
        const connected = () => emit('connected', { local_port: socket.localPort || 0, remote_port: socket.remotePort || 0 })

        if (socket.connecting) { socket.once('connect', connected) } else { connected() }
      })
      // Node finish = flushed to the local socket, not acknowledgement by the hub.
      req.once('finish', () => emit('write_finished'))
      req.once('response', res => {
        emit('response_start', { status: res.statusCode || 0 })
        let bytes = 0
        res.on('data', chunk => { bytes += chunk.length })
        // The JSON consumer registers first; receipt must precede its synchronous
        // decompression/delta decoding/JSON parsing, not include that local work.
        res.prependOnceListener('end', () => emit('response_end', { bytes }))
        res.once('close', () => emit('response_close', { bytes, complete: res.complete }))
      })
      req.once('error', () => emit('error'))
      req.once('close', () => emit('request_close'))
    }
  }
}
