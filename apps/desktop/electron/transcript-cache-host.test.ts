import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import fixture from './fixtures/hub-sync-sessions-prefetch-projection.json'
import { installTranscriptCache, markInboxSessions } from './transcript-cache-host'

const dirs: string[] = []

afterEach(() => {
  for (const dir of dirs.splice(0)) {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

function install(fullResponse = false) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'transcript-cache-host-'))
  dirs.push(dir)

  const on = new Map<string, (event: any, payload?: any) => void>()
  const ipcMain = { handle: vi.fn(), on: (channel: string, fn: any) => on.set(channel, fn) }
  const backendFor = vi.fn(async (connectionId: string, _opts: { passive: boolean }) => ({ connectionId }))

  const getJson = vi.fn(async (descriptor: any, requestPath: string) => {
    if (requestPath.startsWith('/api/plugins/hub-sync/sessions')) {
      if (fullResponse) {
        return { ...fixture.full, sessions: fixture.full.sessions.filter(record => ['old', 'new'].includes(record.id)) }
      }

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

  return { backendFor, getJson, host, on }
}

describe('transcript cache host', () => {
  it('requests the prefetch projection while accepting older servers full responses', async () => {
    const { getJson, host } = install(true)

    try {
      expect(await host.prefetcher.tick()).toMatchObject({ fetched: 2, targets: 2 })
      expect(getJson).toHaveBeenCalledWith(
        { connectionId: 'local' },
        '/api/plugins/hub-sync/sessions?limit=2000&projection=prefetch',
        { timeoutMs: 8_000 }
      )
    } finally {
      host.stop()
    }
  })

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
    // The store is awaited (it runs on a worker thread in the app), so the reply lands after the listener returns.
    await vi.waitFor(() => expect(event.returnValue).toBeDefined())
    expect(event.returnValue?.messages).toEqual([{ content: 'row of local', id: 1, role: 'user' }])
    host.stop()
  })

  it("[daveey] prefetch IPC stores an @session ref's latest rows under its key and skips the fetch when current", async () => {
    const { getJson, host, on } = install()

    const messageReads = () =>
      getJson.mock.calls.filter(([, requestPath]) => String(requestPath).includes('/messages')).length

    const key = { connectionId: 'mac-mini', profile: 'default', sessionId: 'old-ref' }

    on.get('hermes:transcript-cache:prefetch')!({}, key)

    await vi.waitFor(() =>
      expect(host.store.read(key)?.messages).toEqual([{ content: 'row of mac-mini', id: 1, role: 'user' }])
    )
    expect(messageReads()).toBe(1)
    expect(getJson).toHaveBeenCalledWith(
      { connectionId: 'mac-mini' },
      expect.stringMatching(/^\/api\/sessions\/old-ref\/messages\?.*order=latest/),
      expect.anything()
    )

    // Cache current (fresh copy, session not listed as moved): a second ask fetches nothing.
    on.get('hermes:transcript-cache:prefetch')!({}, key)
    await expect(host.prefetcher.prefetchKey(key)).resolves.toBe('current')
    expect(messageReads()).toBe(1)

    // A session the tick already cached at the hub's meta is current too; '' means the primary.
    await host.prefetcher.tick()
    const before = messageReads()
    const prefetchKey = vi.spyOn(host.prefetcher, 'prefetchKey')
    on.get('hermes:transcript-cache:prefetch')!({}, { connectionId: '', profile: '', sessionId: 'l1' })
    expect(prefetchKey).toHaveBeenCalledWith({ connectionId: 'local', profile: 'default', sessionId: 'l1' })
    await expect(prefetchKey.mock.results[0].value).resolves.toBe('current')
    expect(messageReads()).toBe(before)
    host.stop()
  })
})

describe('[daveey] open Inbox cards in prefetch', () => {
  it('flags exactly the hub sessions an open card points at (gateway, profile, id)', () => {
    const records = [
      { gateway: 'hub', id: 's1', profile: 'default' },
      { gateway: 'hub', id: 's2', profile: 'work' },
      { gateway: 'mini', id: 's1', profile: 'default' }
    ]

    const marked = markInboxSessions(records, [
      { gateway: 'hub', profile: 'default', session_id: 's1' },
      { gateway: 'hub', profile: 'default', session_id: 's2' }
    ])

    expect(marked.slice(0, 3).map(record => Boolean(record.inbox))).toEqual([true, false, false])
  })

  it('adds a record for a card whose session the list left out (archived sessions are not listed)', () => {
    const marked = markInboxSessions(
      [{ gateway: 'hub', id: 's1', profile: 'default' }],
      [{ created_at: 7, gateway: 'hub', profile: 'default', session_id: 'archived-card' }]
    )

    expect(marked).toContainEqual({ activity: 7, gateway: 'hub', id: 'archived-card', inbox: true, profile: 'default' })
    expect(marked).toHaveLength(2)
  })

  it("reads the replica Inbox on the primary and fetches a card's session first", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'transcript-cache-host-inbox-'))
    dirs.push(dir)
    const fetched: string[] = []

    const getJson = vi.fn(async (descriptor: any, requestPath: string) => {
      if (requestPath.startsWith('/api/plugins/hub-sync/sessions')) {
        return {
          sessions: [
            { activity: 9, gateway: 'mini', id: 'chat', message_count: 3, profile: 'default' },
            { activity: 1, automated: true, gateway: 'mini', id: 'card', message_count: 3, profile: 'default' }
          ]
        }
      }

      if (requestPath === '/api/plugins/hub-sync/inbox') {
        return { items: [{ gateway: 'mini', profile: 'default', session_id: 'card', state: 'open' }] }
      }

      if (requestPath === '/api/plugins/hub-sync/status') {
        return { gateway: descriptor.connectionId === 'local' ? 'macbook' : 'mini' }
      }

      fetched.push(requestPath.split('/')[3])

      return { messages: [{ content: 'row', id: 1, role: 'user' }] }
    })

    const host = installTranscriptCache({
      backendFor: vi.fn(async (connectionId: string) => ({ connectionId })),
      getJson,
      ipcMain: { handle: vi.fn(), on: vi.fn() } as never,
      log: () => undefined,
      registry: () => ({ connectionIds: ['local', 'mac-mini'], primary: 'local' }),
      userDataDir: dir
    })

    await host.prefetcher.tick()

    expect(getJson).toHaveBeenCalledWith(expect.anything(), '/api/plugins/hub-sync/inbox', expect.anything())
    expect(fetched[0]).toBe('card')
  })
})
