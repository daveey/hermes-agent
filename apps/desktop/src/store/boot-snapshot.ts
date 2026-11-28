/**
 * [daveey] Sidebar boot snapshot (instant session loads, cold start).
 *
 * The first window reads, synchronously from Electron main, this gateway's
 * hermes-hub replica rows (every gateway's sessions, its own included) and the
 * last projects tree a live backend answered. They paint the sidebar before
 * the local backend is up (~6 s on a cold start); live data replaces them per
 * owner as it arrives:
 *
 *  - session rows join the offline fallback inputs (`mergeOfflineFallback`),
 *    which already drops an owner's fallback rows once its live list is fresh;
 *  - the tree seeds `$projectTree` only while it is empty, and the first live
 *    payload replaces it.
 *
 * `[sidebar-timing]` reports, once per launch, ms from process start to the
 * first sidebar rows and to the first live list, and where the rows came from.
 */
import { atom, computed } from 'nanostores'

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import type { SessionInfo } from '@/hermes'

import { $connectionsRegistry } from './connection-registry-state'
import { connectionForHubGateway, hubReplicaConnectionId } from './hub-sync'
import {
  $answeredSessionOwners,
  $offlineSessions,
  offlineIdentity,
  type OfflineReplicaRecord,
  offlineSessionsFromReplica,
  sessionOwnerAnswered
} from './offline-sessions'
import { $projectTree } from './project-tree'

type BootSnapshotWire = NonNullable<
  ReturnType<NonNullable<NonNullable<Window['hermesDesktop']>['bootSnapshot']>['read']>
>

interface BootState {
  appStartMs: null | number
  hubGateway: null | string
  /** The snapshot gave the sidebar something to show (rows or a tree). */
  painted: boolean
  readMs: null | number
  records: OfflineReplicaRecord[]
  /** Epoch seconds the snapshot was read: row freshness (asleep) is judged at this moment. */
  readAtS: number
  treeProjects: number
}

const EMPTY: BootState = {
  appStartMs: null,
  hubGateway: null,
  painted: false,
  readAtS: 0,
  readMs: null,
  records: [],
  treeProjects: 0
}

export const $bootSnapshot = atom<BootState>(EMPTY)

/** Boot rows as sidebar `SessionInfo`; other gateways' rows resolve once the registry is known. */
export const $bootSessions = computed([$bootSnapshot, $connectionsRegistry], (boot): SessionInfo[] => {
  if (!boot.records.length) {
    return []
  }

  const own = boot.hubGateway

  return offlineSessionsFromReplica(
    boot.records,
    gateway => (gateway === own ? hubReplicaConnectionId() : connectionForHubGateway(gateway)),
    boot.readAtS,
    { includeLocal: true }
  )
})

/** The sidebar's fallback inputs: the hub-sync plugin's rows, then boot rows it does not have (plugin rows win).
 *  [daveey] A boot row is a cold-start stand-in only: it is frozen at launch, so once its owner has answered a live
 *  list it is dropped for good. Kept, it resurrected chats archived after launch every time that owner went stale
 *  (the replica drops archived rows, so the boot copy was the only one left to fill the gap). */
export const $sidebarFallbackSessions = computed(
  [$offlineSessions, $bootSessions, $answeredSessionOwners],
  (offline, boot, answered): SessionInfo[] => {
    if (!boot.length) {
      return offline
    }

    const have = new Set(offline.map(offlineIdentity))

    const extra = boot.filter(
      row => !have.has(offlineIdentity(row)) && !sessionOwnerAnswered(row.connection_id, row.profile, answered)
    )

    return extra.length ? [...offline, ...extra] : offline
  }
)

/** Read and apply the snapshot. Main window only; a no-op without the bridge or on any failure. */
export function applyBootSnapshot(
  read: () => BootSnapshotWire | null | undefined = () => window.hermesDesktop?.bootSnapshot?.read?.(),
  nowMs: number = Date.now()
): BootState {
  let snap: BootSnapshotWire | null | undefined

  try {
    snap = read()
  } catch {
    snap = null
  }

  if (!snap) {
    return $bootSnapshot.get()
  }

  const records = (snap.hub?.sessions ?? []) as unknown as OfflineReplicaRecord[]
  const tree = Array.isArray(snap.tree?.tree) ? (snap.tree.tree as SidebarProjectTree[]) : []

  if (tree.length && !$projectTree.get().length) {
    $projectTree.set(tree)
  }

  const state: BootState = {
    appStartMs: snap.appStartMs ?? null,
    hubGateway: snap.hub?.gateway ?? null,
    painted: records.length > 0 || tree.length > 0,
    readAtS: nowMs / 1000,
    readMs: snap.readMs ?? null,
    records,
    treeProjects: tree.length
  }

  $bootSnapshot.set(state)

  return state
}

/** Running state is never snapshotted: a boot row must not claim a turn is running from a past launch. */
const withoutRunning = (tree: readonly SidebarProjectTree[]): unknown[] =>
  JSON.parse(JSON.stringify(tree, (key, value) => (key === 'is_active' ? false : value)))

const SAVE_DEBOUNCE_MS = 3_000
let saveTimer: ReturnType<typeof setTimeout> | undefined

/** Save the live tree for the next cold start (debounced; trees refresh on every sessions.changed). */
export function saveBootTree(connectionId: null | string, activeProjectId: null | string): void {
  const save = window.hermesDesktop?.bootSnapshot?.saveTree

  if (!save) {
    return
  }

  clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    try {
      save({ activeProjectId, connectionId: connectionId ?? '', tree: withoutRunning($projectTree.get()) })
    } catch {
      // best effort: the next cold start just paints from hub rows only
    }
  }, SAVE_DEBOUNCE_MS)
}

// ── [sidebar-timing] ────────────────────────────────────────────────────────

interface SidebarTiming {
  firstRowsMs?: number
  liveMs?: number
  rows?: number
  source?: 'live' | 'snapshot'
}

const timing: SidebarTiming = {}
let timingWritten = false

const sinceStart = (nowMs: number) => {
  const start = $bootSnapshot.get().appStartMs

  return start ? Math.round(nowMs - start) : null
}

function writeTimingIfDone(): void {
  if (timingWritten || timing.firstRowsMs === undefined || timing.liveMs === undefined) {
    return
  }

  timingWritten = true
  const boot = $bootSnapshot.get()

  const line = `[sidebar-timing] ${JSON.stringify({
    ...timing,
    readMs: boot.readMs,
    snapshotRows: boot.records.length,
    snapshotProjects: boot.treeProjects
  })}`

  try {
    console.info(line)
    window.hermesDesktop?.logLine?.(line)
  } catch {
    // logging must never break the sidebar
  }
}

/** The sidebar drew rows for the first time this launch. */
export function markSidebarFirstRows(rows: number, live: boolean, nowMs: number = Date.now()): void {
  if (timing.firstRowsMs !== undefined || rows <= 0) {
    return
  }

  const ms = sinceStart(nowMs)

  if (ms === null) {
    return
  }

  timing.firstRowsMs = ms
  timing.rows = rows
  timing.source = live ? 'live' : 'snapshot'
  writeTimingIfDone()
}

/** The first live session list landed this launch. */
export function markSidebarLive(nowMs: number = Date.now()): void {
  if (timing.liveMs !== undefined) {
    return
  }

  const ms = sinceStart(nowMs)

  if (ms === null) {
    return
  }

  timing.liveMs = ms
  writeTimingIfDone()
}

/** Test seam. */
export function _resetBootSnapshotForTests(): void {
  clearTimeout(saveTimer)
  $bootSnapshot.set(EMPTY)
  delete timing.firstRowsMs
  delete timing.liveMs
  delete timing.rows
  delete timing.source
  timingWritten = false
}
