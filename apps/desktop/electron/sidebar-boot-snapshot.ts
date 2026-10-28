/**
 * [daveey] Sidebar boot snapshot (instant session loads, cold start).
 *
 * The sidebar's rows come from the local Python backend, which takes ~6 s to
 * boot. This module lets the first window paint them before that, from two
 * sources Electron main can read synchronously:
 *
 *  - hub sessions: this gateway's hermes-hub replica (`hub.db`), read
 *    read-only. Same rows the hub-sync plugin's `GET /sessions` serves
 *    (published = this gateway's own, replica = every other gateway's,
 *    pending requests attached, shared-field overrides on top in primary
 *    mode); `hub-sync/hubview.py` View.rows is the reference.
 *  - projects tree: the last tree a live backend answered, saved by the
 *    renderer to `sidebar-boot-snapshot.json` in userData.
 *
 * IPC (preload exposes it as `hermesDesktop.bootSnapshot`):
 *   hermes:boot-snapshot:read       sendSync  -> BootSnapshot
 *   hermes:boot-snapshot:save-tree  send      {tree, activeProjectId, connectionId}
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'

import type { IpcMain } from 'electron'

// hubview.py: STALE_AFTER_S, PENDING_STATES, SHARED["sessions"].
export const HUB_STALE_AFTER_S = 180
const PENDING_STATES = ['local', 'queued']
const SHARED_SESSION_FIELDS = new Set(['title', 'pinned', 'archived', 'hidden'])
export const HUB_SESSIONS_LIMIT = 2000
const TREE_FILE = 'sidebar-boot-snapshot.json'
const TREE_VERSION = 1

export interface HubSessionsSnapshot {
  available: true
  gateway: null | string
  sessions: Record<string, unknown>[]
  stale: boolean
  synced_at: null | number
}

export interface TreeSnapshot {
  activeProjectId: null | string
  connectionId: string
  savedAt: number
  tree: unknown[]
}

export interface BootSnapshot {
  /** Epoch ms the Electron main process started (cold-start timing base). */
  appStartMs: number
  hub: HubSessionsSnapshot | null
  readMs: number
  tree: TreeSnapshot | null
}

export function hubDbPath(env: NodeJS.ProcessEnv = process.env, home = os.homedir()): string {
  const base = env.HERMES_HUB_STATE?.trim() || path.join(home, '.local', 'state', 'hermes-hub')

  return path.join(base.replace(/^~(?=$|\/)/, home), 'hub.db')
}

const parseObject = (text: unknown): null | Record<string, any> => {
  try {
    const value = JSON.parse(String(text))

    return value && typeof value === 'object' && !Array.isArray(value) ? value : null
  } catch {
    return null
  }
}

const parseJson = (text: unknown): unknown => {
  try {
    return JSON.parse(String(text))
  } catch {
    return null
  }
}

const toFloat = (value: unknown): null | number => {
  if (value === null || value === undefined || value === '' || value === 'None') {
    return null
  }

  const n = Number(value)

  return Number.isFinite(n) ? n : null
}

/**
 * hubview.py `sessions(include_archived=False)` against `dbPath`, read-only:
 * null when the db is missing or unreadable (the caller paints nothing).
 */
export function readHubSessions(
  dbPath: string,
  nowS: number = Date.now() / 1000,
  limit: number = HUB_SESSIONS_LIMIT
): HubSessionsSnapshot | null {
  if (!fs.existsSync(dbPath)) {
    return null
  }

  let db: DatabaseSync | null = null

  try {
    // hubview.ro_connect: rw + query_only, because a readOnly open fails on a WAL db whose -wal/-shm are missing.
    // The existsSync above means this never creates the file.
    db = new DatabaseSync(dbPath, { timeout: 500 })
    db.exec('PRAGMA query_only = ON')
    const conn = db

    const has = (table: string) =>
      Boolean(conn.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(table))

    const meta = (key: string): null | string => {
      if (!has('meta')) {
        return null
      }

      const row = conn.prepare('SELECT v FROM meta WHERE k = ?').get(key) as { v?: unknown } | undefined
      const v = row?.v

      return v === null || v === undefined || v === 'None' ? null : String(v)
    }

    const gateway = meta('gateway')
    const lastOk = toFloat(meta('last_ok'))
    const stale = lastOk === null || nowS - lastOk > HUB_STALE_AFTER_S

    const ownerSeen = new Map<string, number>()

    if (has('replica')) {
      for (const r of conn.prepare('SELECT gw, MAX(updated_at) AS t FROM replica GROUP BY gw').all() as any[]) {
        ownerSeen.set(String(r.gw), (Number(r.t) || 0) / 1000)
      }
    }

    const syncedAt = (gw: string): null | number => {
      const seen = ownerSeen.get(gw)

      if (seen === undefined) {
        return lastOk
      }

      return lastOk ? Math.min(seen, lastOk) : seen
    }

    const pending = new Map<string, unknown[]>()

    if (has('sent')) {
      const rows = conn
        .prepare(
          `SELECT id, body, state, created_at FROM sent WHERE state IN (${PENDING_STATES.map(() => '?').join(',')}) ORDER BY created_at`
        )
        .all(...PENDING_STATES) as any[]

      for (const r of rows) {
        const b = parseObject(r.body)

        if (!b || b.coll !== 'sessions') {
          continue
        }

        const k = `${b.to_gw}\u0000${b.key}`
        const list = pending.get(k) ?? []
        list.push({ action: b.action, args: b.args || {}, created_at: r.created_at, id: r.id, state: r.state })
        pending.set(k, list)
      }
    }

    let sharedPrimary = false

    try {
      const raw = parseJson(meta('shared_fields') ?? '"off"')
      const mode = raw && typeof raw === 'object' ? (raw as Record<string, unknown>).sessions : raw
      sharedPrimary = mode === 'primary'
    } catch {
      sharedPrimary = false
    }

    const overrides = new Map<string, Map<string, { by: unknown; value: unknown }>>()

    if (sharedPrimary && has('overrides')) {
      const rows = conn
        .prepare("SELECT gw, key, field, value, by_gw FROM overrides WHERE coll = 'sessions' AND value IS NOT NULL")
        .all() as any[]

      for (const r of rows) {
        if (SHARED_SESSION_FIELDS.has(r.field) || r.field === 'pending_action') {
          const k = `${r.gw}\u0000${r.key}`
          const fields = overrides.get(k) ?? new Map()
          fields.set(String(r.field), { by: r.by_gw, value: parseJson(r.value) })
          overrides.set(k, fields)
        }
      }
    }

    const overlay = (d: Record<string, any>) => {
      const ovr = overrides.get(`${d.gateway}\u0000${d.key}`)

      if (!ovr) {
        return d
      }

      for (const [field, o] of ovr) {
        if (field === 'pending_action') {
          d.pending_action = o.value

          continue
        }

        if (d[field] !== o.value) {
          d.shared = { ...(d.shared ?? {}), [field]: { by: o.by, owner_value: d[field] } }
        }

        d[field] = o.value
      }

      return d
    }

    const out: Record<string, any>[] = []

    if (has('published')) {
      for (const r of conn.prepare("SELECT key, data, version FROM published WHERE coll = 'sessions'").all() as any[]) {
        const d = parseObject(r.data)

        if (d) {
          Object.assign(d, {
            gateway,
            key: r.key,
            local: true,
            pending: pending.get(`${gateway}\u0000${r.key}`) ?? [],
            stale: false,
            synced_at: nowS,
            version: r.version
          })
          out.push(overlay(d))
        }
      }
    }

    if (has('replica')) {
      const rows = conn
        .prepare("SELECT gw, key, data, version, updated_at FROM replica WHERE coll = 'sessions' AND gw IS NOT ?")
        .all(gateway) as any[]

      for (const r of rows) {
        const d = parseObject(r.data)

        if (d) {
          Object.assign(d, {
            gateway: r.gw,
            key: r.key,
            local: false,
            pending: pending.get(`${r.gw}\u0000${r.key}`) ?? [],
            stale,
            synced_at: syncedAt(String(r.gw)),
            updated_at_hub: (Number(r.updated_at) || 0) / 1000,
            version: r.version
          })
          out.push(overlay(d))
        }
      }
    }

    const sessions = out
      .filter(d => !d.archived)
      .sort((a, b) => (Number(b.activity || b.started_at) || 0) - (Number(a.activity || a.started_at) || 0))
      .slice(0, Math.max(1, Math.min(limit, 5000)))

    return { available: true, gateway, sessions, stale, synced_at: lastOk }
  } catch {
    return null
  } finally {
    try {
      db?.close()
    } catch {
      // already closed
    }
  }
}

export function readTreeSnapshot(file: string): TreeSnapshot | null {
  try {
    const raw = parseObject(fs.readFileSync(file, 'utf8'))

    if (!raw || raw.v !== TREE_VERSION || !Array.isArray(raw.tree) || typeof raw.connectionId !== 'string') {
      return null
    }

    return {
      activeProjectId: typeof raw.activeProjectId === 'string' ? raw.activeProjectId : null,
      connectionId: raw.connectionId,
      savedAt: Number(raw.savedAt) || 0,
      tree: raw.tree
    }
  } catch {
    return null
  }
}

/** Atomic replace (tmp + rename) so a crash mid-write never leaves a torn snapshot. */
export function writeTreeSnapshot(file: string, snapshot: Omit<TreeSnapshot, 'savedAt'>, nowMs = Date.now()): void {
  const tmp = `${file}.${process.pid}.tmp`
  fs.writeFileSync(tmp, JSON.stringify({ v: TREE_VERSION, savedAt: nowMs, ...snapshot }))
  fs.renameSync(tmp, file)
}

export interface SidebarBootSnapshotDeps {
  appStartMs: number
  hubDb?: string
  ipcMain: IpcMain
  log: (line: string) => void
  userDataDir: string
}

export function installSidebarBootSnapshot(deps: SidebarBootSnapshotDeps) {
  const treeFile = path.join(deps.userDataDir, TREE_FILE)
  const hubDb = deps.hubDb ?? hubDbPath()

  const read = (): BootSnapshot => {
    const t0 = performance.now()
    let hub: HubSessionsSnapshot | null = null
    let tree: TreeSnapshot | null = null

    try {
      hub = readHubSessions(hubDb)
    } catch (error) {
      deps.log(`hub read failed: ${(error as Error)?.message || error}`)
    }

    try {
      tree = readTreeSnapshot(treeFile)
    } catch (error) {
      deps.log(`tree read failed: ${(error as Error)?.message || error}`)
    }

    return { appStartMs: deps.appStartMs, hub, readMs: Math.round(performance.now() - t0), tree }
  }

  deps.ipcMain.on('hermes:boot-snapshot:read', event => {
    event.returnValue = read()
  })

  deps.ipcMain.on('hermes:boot-snapshot:save-tree', (_event, payload) => {
    if (!payload || !Array.isArray(payload.tree) || typeof payload.connectionId !== 'string') {
      return
    }

    try {
      writeTreeSnapshot(treeFile, {
        activeProjectId: typeof payload.activeProjectId === 'string' ? payload.activeProjectId : null,
        connectionId: payload.connectionId,
        tree: payload.tree
      })
    } catch (error) {
      deps.log(`tree save failed: ${(error as Error)?.message || error}`)
    }
  })

  return { read }
}
