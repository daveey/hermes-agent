/**
 * [daveey] Electron-main host for the local transcript cache (instant session loads, C1):
 * owns the SQLite store, answers the renderer's cache IPC and runs the background
 * prefetcher against every registry gateway. main.ts injects its backend routing
 * (ensureRegistryBackend + getJsonForBackend) so this module never dials on its own.
 *
 * The store itself runs on a worker thread (`spawnWorker`, transcript-cache-client.ts): its
 * SQLite calls are synchronous, and on the main thread a write that waited on the disk froze
 * the whole app for 1-1.8 s. Everything here awaits the client; the renderer's sync read is
 * answered when the worker replies (the renderer blocks on it either way, the main loop does not).
 *
 * IPC (preload exposes it as `hermesDesktop.transcriptCache`):
 *   hermes:transcript-cache:read             sendSync  {connectionId, profile, sessionId} -> entry | null
 *   hermes:transcript-cache:put              send      {key, messages, kind: 'latest' | 'older'}
 *   hermes:transcript-cache:drop             send      {key} | {sessionId, everywhere: true}
 *   hermes:transcript-cache:migrate-profile  send      {connectionId, from, to}
 *   hermes:transcript-cache:clear            send
 *   hermes:transcript-cache:stats            invoke    -> {bytes, sessions}
 *   hermes:transcript-cache:prefetch         send      {connectionId, profile, sessionId}  ([daveey] @session ref warm-up)
 *   hermes:transcript-cache:sessions-moved   send      {connectionId, sessions}  ([daveey] a gateway's `sessions.moved` push)
 */
import path from 'node:path'

import type { IpcMain } from 'electron'

import { type TranscriptCacheKey, TranscriptCacheStore } from './transcript-cache'
import {
  inlineClient,
  spawnStoreWorker,
  type StoreOp,
  type StoreWorkerPort,
  type TranscriptCacheClient,
  workerClient
} from './transcript-cache-client'
import { type DeltaPage, type HubSessionRecord, TranscriptPrefetcher } from './transcript-prefetch'

export interface TranscriptCacheHostDeps {
  /** Resolve the backend descriptor of a registry connection's default profile. */
  backendFor: (connectionId: string, opts: { passive: boolean }) => Promise<unknown>
  getJson: (descriptor: unknown, requestPath: string, opts: { timeoutMs: number }) => Promise<any>
  ipcMain: IpcMain
  log: (line: string) => void
  /** True while a connection's link is slow: the background prefetch pass skips it (slow-link.ts). */
  prefetchPaused?: (connectionId: string) => boolean
  /** Registry snapshot: the primary id ('' connection ids mean it) and every connection id. */
  registry: () => { connectionIds: string[]; primary: string }
  /** Tests: start the store worker themselves. The app passes `workerFile` instead. */
  spawnWorker?: (file: string) => StoreWorkerPort
  userDataDir: string
  /**
   * The built worker script (dist/electron-transcript-cache-worker.js) that owns the cache file.
   * Omitted (tests) or unusable: the store runs in this thread.
   */
  workerFile?: string
}

/** The registry as this module reads it (main.ts keeps the registry file's own shape). */
export const registrySnapshot = (registry: { connections: readonly { id: string }[]; primary: string }) => ({
  connectionIds: registry.connections.map(connection => connection.id),
  primary: registry.primary
})

/** The renderer's sync read gives up (a cache miss: it falls back to REST) after this long. */
export const READ_TIMEOUT_MS = 1_500

const GATEWAY_NAME_PREFIX = 'hub-gateway:'
const HUB_LIST_LIMIT = 2000

const inboxKey = (gateway: unknown, profile: unknown, sessionId: unknown) =>
  `${String(gateway ?? '')}\u0000${String(profile ?? '').trim() || 'default'}\u0000${String(sessionId ?? '')}`

/** An open Inbox card, as the hub-sync `/inbox` route serves it. */
export interface OpenInboxItem {
  created_at?: null | number
  gateway: string
  profile?: null | string
  session_id: string
  updated_at?: null | number
}

/**
 * [daveey] Flag the hub session records an open Inbox card points at (the prefetcher fetches those first, even
 * hidden or archived ones: a card's session usually is). A card whose session the list left out (archived rows
 * are not listed) gets a minimal record of its own, so it is prefetched too.
 */
export function markInboxSessions(records: HubSessionRecord[], inbox: readonly OpenInboxItem[]): HubSessionRecord[] {
  if (!inbox.length) {
    return records
  }

  const keys = new Set(inbox.map(item => inboxKey(item.gateway, item.profile, item.session_id)))
  const listed = new Set<string>()

  const marked = records.map(record => {
    const key = inboxKey(record.gateway, record.profile, record.id)

    if (!keys.has(key)) {
      return record
    }

    listed.add(key)

    return { ...record, inbox: true }
  })

  for (const item of inbox) {
    const key = inboxKey(item.gateway, item.profile, item.session_id)

    if (!listed.has(key)) {
      listed.add(key)
      marked.push({
        activity: item.updated_at ?? item.created_at ?? null,
        gateway: item.gateway,
        id: item.session_id,
        inbox: true,
        profile: item.profile ?? 'default'
      })
    }
  }

  return marked
}

export function installTranscriptCache(deps: TranscriptCacheHostDeps) {
  const file = path.join(deps.userDataDir, 'transcript-cache.sqlite')
  let inlineStore: null | TranscriptCacheStore = null

  const openInline = (): TranscriptCacheClient => {
    inlineStore = new TranscriptCacheStore(file)

    return inlineClient(inlineStore, deps.log)
  }

  const { workerFile } = deps
  const spawnWorker = deps.spawnWorker ?? (workerFile ? (cache: string) => spawnStoreWorker(workerFile, cache) : null)

  const client: TranscriptCacheClient = spawnWorker
    ? workerClient({ fallback: openInline, log: deps.log, spawn: () => spawnWorker(file) })
    : openInline()

  const normalizeKey = (raw: any): TranscriptCacheKey | null => {
    const sessionId = String(raw?.sessionId ?? '').trim()

    if (!sessionId) {
      return null
    }

    return {
      connectionId: String(raw?.connectionId ?? '').trim() || deps.registry().primary || 'local',
      profile: String(raw?.profile ?? '').trim() || 'default',
      sessionId
    }
  }

  // Remote gateways are held warm by the B prewarmer (remote-prewarm); prefetch only
  // rides a pool entry that already exists and never starts a cold SSH dial or spawn
  // (and, being passive, never counts as an open). The local backend may be started.
  const dialOptions = (connectionId: string) => ({
    passive: connectionId !== (deps.registry().primary || 'local')
  })

  /** Fire-and-forget store call: a failure is logged, never thrown out of an IPC listener. */
  const send = (label: string, op: StoreOp, args: unknown[]) => {
    void client.call(op, args).catch(error => {
      deps.log(`transcript cache ${label} failed: ${(error as Error)?.message || error}`)
    })
  }

  deps.ipcMain.on('hermes:transcript-cache:read', (event, raw) => {
    const key = normalizeKey(raw)

    // Sync IPC: the renderer stays blocked until returnValue is set, so every path sets it once.
    const reply = (value: unknown) => {
      try {
        event.returnValue = value
      } catch {
        // the window went away while the read was in flight
      }
    }

    if (!key) {
      reply(null)

      return
    }

    client.call('read', [key], { timeoutMs: READ_TIMEOUT_MS }).then(
      entry => reply(entry ?? null),
      error => {
        deps.log(`transcript cache read failed: ${(error as Error)?.message || error}`)
        reply(null)
      }
    )
  })

  deps.ipcMain.on('hermes:transcript-cache:put', (_event, payload) => {
    const key = normalizeKey(payload?.key)

    if (!key || !Array.isArray(payload?.messages)) {
      return
    }

    send('put', payload.kind === 'older' ? 'putOlder' : 'putLatest', [key, payload.messages])
  })

  deps.ipcMain.on('hermes:transcript-cache:drop', (_event, payload) => {
    if (payload?.everywhere) {
      const sessionId = String(payload.sessionId ?? '').trim()

      if (sessionId) {
        send('drop', 'dropEverywhere', [sessionId])
      }

      return
    }

    const key = normalizeKey(payload?.key)

    if (key) {
      send('drop', 'drop', [key])
    }
  })

  deps.ipcMain.on('hermes:transcript-cache:migrate-profile', (_event, payload) => {
    const connectionId = String(payload?.connectionId ?? '').trim() || deps.registry().primary || 'local'
    const from = String(payload?.from ?? '').trim() || 'default'
    const to = String(payload?.to ?? '').trim() || 'default'

    send('migrate', 'migrateProfile', [connectionId, from, to])
  })

  deps.ipcMain.on('hermes:transcript-cache:clear', () => {
    send('clear', 'clear', [])
  })

  deps.ipcMain.handle('hermes:transcript-cache:stats', () => client.call('stats'))

  // Hub gateway name -> registry connection id, learned from each connection's own
  // hub-sync status and remembered in the cache file so it survives restarts.
  const learnedThisRun = new Set<string>()

  const connectionForGateway = async (gateway: string): Promise<string | undefined> => {
    const { connectionIds } = deps.registry()
    const remembered = await client.call<null | string>('getMeta', [GATEWAY_NAME_PREFIX + gateway])

    if (remembered && connectionIds.includes(remembered)) {
      return remembered
    }

    for (const connectionId of connectionIds) {
      if (learnedThisRun.has(connectionId)) {
        continue
      }

      learnedThisRun.add(connectionId)

      try {
        const descriptor = await deps.backendFor(connectionId, dialOptions(connectionId))
        const status = await deps.getJson(descriptor, '/api/plugins/hub-sync/status', { timeoutMs: 8_000 })
        const name = typeof status?.gateway === 'string' ? status.gateway.trim() : ''

        if (name) {
          await client.call('setMeta', [GATEWAY_NAME_PREFIX + name, connectionId])

          if (name === gateway) {
            return connectionId
          }
        }
      } catch (error) {
        learnedThisRun.delete(connectionId) // retried on a later tick
        deps.log(`transcript prefetch: could not read ${connectionId} hub name (${(error as Error)?.message || error})`)
      }
    }

    return undefined
  }

  // Open Inbox items of every gateway, from this Mac's replica; an unreadable Inbox only drops the priority.
  const openInboxSessions = async (descriptor: unknown): Promise<OpenInboxItem[]> => {
    try {
      const body = await deps.getJson(descriptor, '/api/plugins/hub-sync/inbox', { timeoutMs: 8_000 })
      const items = Array.isArray(body?.items) ? body.items : []

      return items.filter(
        (item: any) => typeof item?.session_id === 'string' && item.session_id && typeof item?.gateway === 'string'
      )
    } catch {
      return []
    }
  }

  const prefetcher = new TranscriptPrefetcher({
    connectionForGateway,
    paused: deps.prefetchPaused,
    // [daveey] Push delta: the rows after the cached tail. A gateway without the route answers a plain page with no
    // `delta` block (an older core ignores the unknown query param): null, and the prefetcher stops asking it.
    fetchAfter: async (key, afterId, limit): Promise<DeltaPage | null> => {
      const descriptor = await deps.backendFor(key.connectionId, dialOptions(key.connectionId))
      const query = new URLSearchParams({ after_id: String(afterId), limit: String(limit), profile: key.profile })

      const page = await deps.getJson(
        descriptor,
        `/api/sessions/${encodeURIComponent(key.sessionId)}/messages?${query.toString()}`,
        { timeoutMs: 30_000 }
      )

      const delta = page?.delta

      if (!delta || typeof delta !== 'object' || !Array.isArray(page?.messages)) {
        return null
      }

      return {
        anchorFound: delta.anchor_found === true,
        foldConflict: delta.fold_conflict === true,
        hasMore: delta.has_more === true,
        messages: page.messages,
        sessionId: typeof page.session_id === 'string' ? page.session_id : key.sessionId,
        transcriptRevision: typeof delta.transcript_revision === 'number' ? delta.transcript_revision : null
      }
    },
    fetchLatest: async (key, limit) => {
      const descriptor = await deps.backendFor(key.connectionId, dialOptions(key.connectionId))

      const query = new URLSearchParams({
        include_compacted: 'true',
        limit: String(limit),
        order: 'latest',
        profile: key.profile
      })

      const page = await deps.getJson(
        descriptor,
        `/api/sessions/${encodeURIComponent(key.sessionId)}/messages?${query.toString()}`,
        { timeoutMs: 30_000 }
      )

      return Array.isArray(page?.messages) ? page.messages : []
    },
    listHubSessions: async () => {
      try {
        // The replica lives on this Mac: read it from the primary without spawning one.
        const descriptor = await deps.backendFor(deps.registry().primary || 'local', { passive: true })

        const body = await deps.getJson(
          descriptor,
          `/api/plugins/hub-sync/sessions?limit=${HUB_LIST_LIMIT}&projection=prefetch`,
          { timeoutMs: 8_000 }
        )

        if (body?.available === false || !Array.isArray(body?.sessions)) {
          return null
        }

        return markInboxSessions(body.sessions as HubSessionRecord[], await openInboxSessions(descriptor))
      } catch {
        return null
      }
    },
    log: deps.log,
    store: {
      meta: key => client.call('meta', [key]),
      metaMany: keys => client.call('metaMany', [keys]),
      putAfter: (key, anchorId, rows, meta) => client.call<boolean>('putAfter', [key, anchorId, rows, meta]),
      putLatest: (key, latest, meta) => client.call<boolean>('putLatest', [key, latest, meta]),
      tailId: key => client.call<null | number>('tailId', [key])
    }
  })

  // [daveey] An `@session` ref chip rendered/hovered: warm that session so its first click paints from cache.
  deps.ipcMain.on('hermes:transcript-cache:prefetch', (_event, raw) => {
    const key = normalizeKey(raw)

    if (key) {
      void prefetcher.prefetchKey(key).catch(error => deps.log(`transcript prefetch (ref) failed: ${error}`))
    }
  })

  // [daveey] A renderer forwards each gateway socket's `sessions.moved` frame; '' names the registry primary.
  deps.ipcMain.on('hermes:transcript-cache:sessions-moved', (_event, payload) => {
    const connectionId = String(payload?.connectionId ?? '').trim() || deps.registry().primary || 'local'

    void prefetcher.sessionsMoved(connectionId, payload?.sessions)
  })

  return {
    client,
    prefetcher,
    start: () => prefetcher.start(),
    stop: () => {
      prefetcher.stop()

      try {
        client.close()
      } catch (error) {
        deps.log(`transcript cache close failed: ${(error as Error)?.message || error}`)
      }
    },
    /** The in-thread store, or null while a worker thread owns the file. */
    get store() {
      return inlineStore
    }
  }
}
