/**
 * [daveey] Electron-main host for the local transcript cache (instant session loads, C1):
 * owns the SQLite store, answers the renderer's cache IPC and runs the background
 * prefetcher against every registry gateway. main.ts injects its backend routing
 * (ensureRegistryBackend + getJsonForBackend) so this module never dials on its own.
 *
 * IPC (preload exposes it as `hermesDesktop.transcriptCache`):
 *   hermes:transcript-cache:read             sendSync  {connectionId, profile, sessionId} -> entry | null
 *   hermes:transcript-cache:put              send      {key, messages, kind: 'latest' | 'older'}
 *   hermes:transcript-cache:drop             send      {key} | {sessionId, everywhere: true}
 *   hermes:transcript-cache:migrate-profile  send      {connectionId, from, to}
 *   hermes:transcript-cache:clear            send
 *   hermes:transcript-cache:stats            invoke    -> {bytes, sessions}
 */
import path from 'node:path'

import type { IpcMain } from 'electron'

import { type TranscriptCacheKey, TranscriptCacheStore } from './transcript-cache'
import { type HubSessionRecord, TranscriptPrefetcher } from './transcript-prefetch'

export interface TranscriptCacheHostDeps {
  /** Resolve the backend descriptor of a registry connection's default profile. */
  backendFor: (connectionId: string, opts: { passive: boolean }) => Promise<unknown>
  getJson: (descriptor: unknown, requestPath: string, opts: { timeoutMs: number }) => Promise<any>
  ipcMain: IpcMain
  log: (line: string) => void
  /** Registry snapshot: the primary id ('' connection ids mean it) and every connection id. */
  registry: () => { connectionIds: string[]; primary: string }
  userDataDir: string
}

const GATEWAY_NAME_PREFIX = 'hub-gateway:'
const HUB_LIST_LIMIT = 2000

export function installTranscriptCache(deps: TranscriptCacheHostDeps) {
  const store = new TranscriptCacheStore(path.join(deps.userDataDir, 'transcript-cache.sqlite'))

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

  const guarded = <T>(label: string, fn: () => T, fallback: T): T => {
    try {
      return fn()
    } catch (error) {
      deps.log(`transcript cache ${label} failed: ${(error as Error)?.message || error}`)

      return fallback
    }
  }

  deps.ipcMain.on('hermes:transcript-cache:read', (event, raw) => {
    const key = normalizeKey(raw)
    event.returnValue = key ? guarded('read', () => store.read(key), null) : null
  })

  deps.ipcMain.on('hermes:transcript-cache:put', (_event, payload) => {
    const key = normalizeKey(payload?.key)

    if (!key || !Array.isArray(payload?.messages)) {
      return
    }

    guarded(
      'put',
      () => (payload.kind === 'older' ? store.putOlder(key, payload.messages) : store.putLatest(key, payload.messages)),
      false
    )
  })

  deps.ipcMain.on('hermes:transcript-cache:drop', (_event, payload) => {
    if (payload?.everywhere) {
      const sessionId = String(payload.sessionId ?? '').trim()

      if (sessionId) {
        guarded('drop', () => store.dropEverywhere(sessionId), undefined)
      }

      return
    }

    const key = normalizeKey(payload?.key)

    if (key) {
      guarded('drop', () => store.drop(key), undefined)
    }
  })

  deps.ipcMain.on('hermes:transcript-cache:migrate-profile', (_event, payload) => {
    const connectionId = String(payload?.connectionId ?? '').trim() || deps.registry().primary || 'local'
    const from = String(payload?.from ?? '').trim() || 'default'
    const to = String(payload?.to ?? '').trim() || 'default'

    guarded('migrate', () => store.migrateProfile(connectionId, from, to), undefined)
  })

  deps.ipcMain.on('hermes:transcript-cache:clear', () => {
    guarded('clear', () => store.clear(), undefined)
  })

  deps.ipcMain.handle('hermes:transcript-cache:stats', () => store.stats())

  // Hub gateway name -> registry connection id, learned from each connection's own
  // hub-sync status and remembered in the cache file so it survives restarts.
  const learnedThisRun = new Set<string>()

  const connectionForGateway = async (gateway: string): Promise<string | undefined> => {
    const { connectionIds } = deps.registry()
    const remembered = store.getMeta(GATEWAY_NAME_PREFIX + gateway)

    if (remembered && connectionIds.includes(remembered)) {
      return remembered
    }

    for (const connectionId of connectionIds) {
      if (learnedThisRun.has(connectionId)) {
        continue
      }

      learnedThisRun.add(connectionId)

      try {
        const descriptor = await deps.backendFor(connectionId, { passive: false })
        const status = await deps.getJson(descriptor, '/api/plugins/hub-sync/status', { timeoutMs: 8_000 })
        const name = typeof status?.gateway === 'string' ? status.gateway.trim() : ''

        if (name) {
          store.setMeta(GATEWAY_NAME_PREFIX + name, connectionId)

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

  const prefetcher = new TranscriptPrefetcher({
    connectionForGateway,
    fetchLatest: async (key, limit) => {
      const descriptor = await deps.backendFor(key.connectionId, { passive: false })

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

        const body = await deps.getJson(descriptor, `/api/plugins/hub-sync/sessions?limit=${HUB_LIST_LIMIT}`, {
          timeoutMs: 8_000
        })

        return body?.available === false || !Array.isArray(body?.sessions) ? null : (body.sessions as HubSessionRecord[])
      } catch {
        return null
      }
    },
    log: deps.log,
    store
  })

  return {
    prefetcher,
    start: () => prefetcher.start(),
    stop: () => {
      prefetcher.stop()
      guarded('close', () => store.close(), undefined)
    },
    store
  }
}
