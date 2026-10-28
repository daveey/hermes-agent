import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { installTranscriptCache } from './transcript-cache-host'

const dirs: string[] = []

afterEach(() => {
  for (const dir of dirs.splice(0)) {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

function install() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'transcript-cache-host-'))
  dirs.push(dir)

  const on = new Map<string, (event: any, payload?: any) => void>()
  const ipcMain = { handle: vi.fn(), on: (channel: string, fn: any) => on.set(channel, fn) }
  const backendFor = vi.fn(async (connectionId: string, _opts: { passive: boolean }) => ({ connectionId }))

  const getJson = vi.fn(async (descriptor: any, requestPath: string) => {
    if (requestPath.startsWith('/api/plugins/hub-sync/sessions')) {
      return {
        sessions: [
          { activity: 2, gateway: 'mini', id: 'm1', message_count: 3, profile: 'default' },
          { activity: 1, gateway: 'macbook', id: 'l1', message_count: 3, profile: 'default' }
        ]
      }
    }

    if (requestPath === '/api/plugins/hub-sync/status') {
      return { gateway: descriptor.connectionId === 'local' ? 'macbook' : 'mini' }
    }

    return { messages: [{ content: `row of ${descriptor.connectionId}`, id: 1, role: 'user' }] }
  })

  const host = installTranscriptCache({
    backendFor,
    getJson,
    ipcMain: ipcMain as never,
    log: () => undefined,
    registry: () => ({ connectionIds: ['local', 'mac-mini'], primary: 'local' }),
    userDataDir: dir
  })

  return { backendFor, host, on }
}

describe('transcript cache host', () => {
  it('prefetch rides warm remote pool entries (passive) and may start the local backend', async () => {
    const { backendFor, host } = install()

    expect(await host.prefetcher.tick()).toMatchObject({ fetched: 2 })

    const remote = backendFor.mock.calls.filter(([id]) => id === 'mac-mini')
    const local = backendFor.mock.calls.filter(([id]) => id === 'local')
    expect(remote.length).toBeGreaterThan(0)
    expect(remote.every(([, opts]) => opts.passive === true)).toBe(true)
    // The hub list read is passive; the local name probe and transcript fetch may spawn.
    expect(local.some(([, opts]) => opts.passive === false)).toBe(true)
    host.stop()
  })

  it('answers the renderer read over sync IPC, normalizing an empty connection to the primary', async () => {
    const { host, on } = install()

    await host.prefetcher.tick()

    const event: { returnValue?: any } = {}
    on.get('hermes:transcript-cache:read')!(event, { connectionId: '', profile: '', sessionId: 'l1' })
    expect(event.returnValue?.messages).toEqual([{ content: 'row of local', id: 1, role: 'user' }])
    host.stop()
  })
})
