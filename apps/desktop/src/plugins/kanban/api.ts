/**
 * Kanban data layer. Everything goes through `ctx.rest` — the plugin's own
 * `/api/plugins/kanban/*` FastAPI router (`plugins/kanban/dashboard/plugin_api.py`),
 * reused as-is via the desktop's namespace-scoped REST door. No new backend.
 *
 * Fetching, caching, polling, dedupe, and invalidation are React Query's job
 * (the app's standard, via the SDK). This module owns the query keys, the REST
 * calls, and the selected-board atom — every call passes `?board=<slug>` so the
 * desktop's selection never flips the server-wide current-board pointer.
 *
 * Every query key and the persisted board selection are scoped by the ACTIVE
 * CONNECTION (`host.state.connectionId`): a board lives on ONE gateway, so a
 * connection switch must be a clean cache miss (the hermes-bots roster
 * pattern), and each gateway remembers its own selected board instead of
 * pinning a slug the next gateway 404s on.
 */

import {
  atom,
  captureGatewayFileDownload,
  host,
  hubGatewayForConnection,
  hubGatewayTasks,
  hubRequest,
  type HubTask,
  learnHubGatewayName,
  type PluginOs,
  type PluginRestOptions,
  type PluginStorage,
  type PluginTranslate,
  queryClient,
  useValue
} from '@hermes/plugin-sdk'

// Native completion notification.
import {
  bindCompletionNotify,
  type CompletionEvent,
  type CompletionNotifyQuiet,
  onKanbanEventsFrame
} from './completion-notify'
import type {
  BoardExportResult,
  BoardImportResult,
  BoardMeta,
  BoardsResponse,
  KanbanBoard,
  KanbanColumn,
  KanbanProfile,
  KanbanProject,
  KanbanTask,
  KanbanTaskDetail,
  OrchestrationSettings,
  TaskEstimate,
  WorkerLog
} from './types'

type Rest = <T>(path: string, opts?: PluginRestOptions) => Promise<T>
type Socket = (path: string, onMessage: (data: unknown) => void) => () => void

let rest: null | Rest = null
let os: null | PluginOs = null

/** Selected board slug ('' = the server's current board). Persisted. */
export const $boardSlug = atom<string>('')

/** Pinned board slugs, in tab order, for the active connection. Persisted. */
export const $pinnedBoards = atom<string[]>([])

export function setBoardPinned(slug: string, pinned: boolean): void {
  const current = $pinnedBoards.get()

  if (current.includes(slug) !== pinned) {
    $pinnedBoards.set(pinned ? [...current, slug] : current.filter(item => item !== slug))
  }
}

/** Move `slug` into `target`'s position. */
export function movePinnedBoard(slug: string, target: string): void {
  const next = [...$pinnedBoards.get()]
  const from = next.indexOf(slug)
  const to = next.indexOf(target)

  if (from < 0 || to < 0 || from === to) {
    return
  }

  next.splice(from, 1)
  next.splice(to, 0, slug)
  $pinnedBoards.set(next)
}

/** Whether the "how this board works" intro was dismissed. Persisted. */
export const $introDismissed = atom<boolean>(false)

/** Sub-group the Running lane by assignee (the dashboard's "lanes by
 *  profile"). Persisted. */
export const $lanesByProfile = atom<boolean>(false)

/** Per-lane collapse OVERRIDES (true=collapsed, false=expanded). Absence means
 *  auto: empty lanes collapse to a rail, occupied lanes expand. Persisted. */
export const $collapsedLanes = atom<Record<string, boolean>>({})

/** Cache scope of the local pool — the SDK atom's own spelling. */
const LOCAL_SCOPE = 'local'

const KANBAN_KEY_ROOT = ['kanban'] as const

const BOARD_SLUG_KEY = 'boardSlug'
const GATEWAY_FILTER_KEY = 'gatewayFilter'
const PINNED_BOARDS_KEY = 'pinnedBoards'
const INTRO_KEY = 'introDismissed'
const LANES_KEY = 'lanesByProfile'
const COLLAPSED_KEY = 'collapsedLanes'

// Last frame cursor per (connection, board) this plugin bind. The socket
// reopens on every board switch and connection change; resuming from the last
// frame replays only what was missed. Keyed by connection so one gateway's
// cursor cannot resume another's stream. Cleared on bind/unbind — events that
// land while the plugin is unloaded are not replayed.
const eventCursorByBoard = new Map<string, number>()

function cursorKey(scope: string, slug: string): string {
  return `${scope}\0${slug}`
}

/** [daveey] A snapshot's tail is a usable `since` only when positive. A merged
 *  board whose routed gateway did not answer carries `latest_event_id: 0`
 *  (mergeBoards), and `since=0` makes the server replay that board's whole
 *  task_events history, 200 rows per frame. On 2026-11-13 the hub's own /board
 *  read timed out, the cached merge said 0, and the reopened socket replayed
 *  ~360k hub events: every frame invalidated the board, which re-read three
 *  gateways ~4x/s and timed out ~1.5k reads per 30 min. Unknown opens at the
 *  server's tail instead (an empty board's tail is 0 anyway). */
function knownCursor(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : undefined
}

function snapshotCursor(scope: string, slug: string): number | undefined {
  for (const archived of [false, true]) {
    const board = queryClient.getQueryData<KanbanBoard>(boardKey(scope, slug, archived))
    const cursor = knownCursor(board?.latest_event_id)

    if (cursor !== undefined) {
      return cursor
    }
  }

  return undefined
}

/** Cursor a fresh socket starts from: the last frame this connection saw, else
 *  the cached board snapshot's tail. Undefined means nothing is known yet —
 *  fetch the snapshot before opening, never open at since=0. */
function eventsSince(scope: string, slug: string): number | undefined {
  const seen = eventCursorByBoard.get(cursorKey(scope, slug))

  if (typeof seen === 'number') {
    return seen
  }

  return snapshotCursor(scope, slug)
}

function eventsUrl(slug: string, since: number | undefined): string {
  const params = new URLSearchParams()

  if (slug) {
    params.set('board', slug)
  }

  if (since !== undefined) {
    params.set('since', String(since))
  }

  const query = params.toString()

  return query ? `/events?${query}` : '/events'
}

function boardSnapshotPath(slug: string): string {
  return slug ? `/board?board=${encodeURIComponent(slug)}` : '/board'
}

/** Cache-scope id for the active connection — the segment every query key
 *  embeds. `'local'` covers the pre-descriptor null; the SDK atom already
 *  reports 'local' for the local pool. For NON-rendering code (mutations,
 *  socket frames); rendering components use `useKanbanScope` so the keys they
 *  build during render recompute when the connection changes. */
export function kanbanConnectionScope(): string {
  return host.state.connectionId.get() ?? LOCAL_SCOPE
}

export function useKanbanScope(): string {
  return useValue(host.state.connectionId) ?? LOCAL_SCOPE
}

/** Where a request issued NOW is routed, as a cache scope. The request tag
 *  moves before the connection descriptor publishes, and React re-keys the
 *  observers later still — so between the two an observer can sit on the
 *  outgoing scope's key while a fetch would land on the incoming backend. */
const routedScope = (): string => host.activeConnectionId() ?? LOCAL_SCOPE

/** `enabled` for every kanban query: only fetch while the key's scope is the
 *  routed one. A switch's app-wide invalidation then leaves the outgoing
 *  observers alone (the incoming keys are already a cache miss) instead of
 *  writing the new gateway's payload under the old connection's key — which
 *  would paint on the way back. Installed as the `['kanban']` query default in
 *  `bindApi`; sites with their own `enabled` compose it. */
export const routedToScope = (query: { queryKey: readonly unknown[] }): boolean => query.queryKey[2] === routedScope()

/** [daveey] Board-wide reads (merged board, switcher counts, Blocked section)
 *  fan out to every gateway and can take seconds over an SSH tunnel, so event
 *  frames refresh them at most once per this interval per connection: the first
 *  frame immediately, any later ones as one trailing refresh. A busy or
 *  catching-up stream (200 events per 300 ms frame) used to invalidate them on
 *  every frame; the reads' queryFns ignore the abort signal, so each superseded
 *  read kept its HTTP request running and they queued past the request
 *  deadline (2026-11-13: ~4 board re-reads/s, ~1.5k timeouts per 30 min). */
export const EVENTS_BOARD_REFRESH_MIN_MS = 2000

const boardRefreshAt = new Map<string, number>()
const boardRefreshTimers = new Map<string, ReturnType<typeof setTimeout>>()
/** Board-wide refreshes waiting for a read already in flight to settle, by query key. */
const boardRefreshWaits = new Map<string, () => void>()

/** [daveey] Invalidate `queryKey` now, or right after the read in flight on it settles.
 *  Never cancel that read (its HTTP request would stay on the wire) and never join it
 *  either: with cancelRefetch: false an invalidation of a fetching query reuses its
 *  promise, and that read's success clears isInvalidated, so events newer than its
 *  server snapshot would wait for the next frame or the 15 s/60 s board poll. At most
 *  one wait per key; a refresh requested meanwhile is already covered by it. */
function invalidateAfterInFlight(scope: string, queryKey: readonly unknown[]): void {
  if (queryClient.isFetching({ queryKey }) === 0) {
    void queryClient.invalidateQueries({ queryKey }, { cancelRefetch: false })

    return
  }

  const id = JSON.stringify(queryKey)

  if (boardRefreshWaits.has(id)) {
    return
  }

  const unsubscribe = queryClient.getQueryCache().subscribe(() => {
    if (queryClient.isFetching({ queryKey }) > 0) {
      return
    }

    unsubscribe()
    boardRefreshWaits.delete(id)
    // The deferred re-read starts the coalescing interval over, so a steady stream
    // of frames over a slow tunnel still re-reads at most once per interval.
    boardRefreshAt.set(scope, Date.now())
    // Out of the settling query's dispatch; a read that started in between is waited on again.
    void Promise.resolve().then(() => invalidateAfterInFlight(scope, queryKey))
  })

  boardRefreshWaits.set(id, unsubscribe)
}

function refreshBoardReads(scope: string): void {
  boardRefreshAt.set(scope, Date.now())
  invalidateAfterInFlight(scope, boardKeyPrefix(scope))
  // Any event can change a board's card count — keep the switcher badge honest.
  invalidateAfterInFlight(scope, boardsKey(scope))
  // [daveey] The sidebar's Blocked section spans every board.
  invalidateAfterInFlight(scope, blockedKey(scope))
}

function scheduleBoardRefresh(scope: string): void {
  if (boardRefreshTimers.has(scope)) {
    return
  }

  const waitMs = (boardRefreshAt.get(scope) ?? -Infinity) + EVENTS_BOARD_REFRESH_MIN_MS - Date.now()

  if (waitMs <= 0) {
    refreshBoardReads(scope)

    return
  }

  boardRefreshTimers.set(
    scope,
    setTimeout(() => {
      boardRefreshTimers.delete(scope)
      refreshBoardReads(scope)
    }, waitMs)
  )
}

function resetBoardRefresh(): void {
  boardRefreshTimers.forEach(timer => clearTimeout(timer))
  boardRefreshTimers.clear()
  boardRefreshWaits.forEach(unsubscribe => unsubscribe())
  boardRefreshWaits.clear()
  boardRefreshAt.clear()
}

/** One live `task_events` frame → precise cache invalidation: the board
 *  (coalesced, see EVENTS_BOARD_REFRESH_MIN_MS), plus each touched task's
 *  detail. The polls stay as the fallback (board 15 s when it spans gateways, else 60 s; drawer task 30 s, its log
 *  3 s while running, else 15 s); the socket just makes the board feel instant. */
function onEventsFrame(scope: string, slug: string, data: unknown, selectedSlug = slug): void {
  const frame = data as { cursor?: unknown; events?: CompletionEvent[] }

  if (typeof frame?.cursor === 'number') {
    eventCursorByBoard.set(cursorKey(scope, slug), frame.cursor)
  }

  const events = frame?.events

  if (!events?.length) {
    return
  }

  scheduleBoardRefresh(scope)

  for (const taskId of new Set(events.map(event => event.task_id).filter(Boolean))) {
    void queryClient.invalidateQueries({ queryKey: taskKey(scope, slug, taskId!) })

    if (selectedSlug !== slug) {
      void queryClient.invalidateQueries({ queryKey: taskKey(scope, selectedSlug, taskId!) })
    }
  }

  // Completion notification (after invalidation so notify failure
  // never interferes with cache invalidation).
  void onKanbanEventsFrame(slug, events).catch(() => undefined)
}

// A persisted, subscribable atom (the structural slice we need — avoids
// importing nanostore's type just to describe one).
interface Persisted<T> {
  get(): T
  set(value: T): void
  listen(cb: (value: T) => void): () => void
}

/** Bind the plugin's doors at register time and return a disposer the host
 *  runs on unload/disable — so nothing (store sync, socket) survives a toggle
 *  or duplicates on re-enable. The events socket is pinned to a board at
 *  handshake, so a board switch closes + reopens it. */
export function bindApi(
  r: Rest,
  storage: PluginStorage,
  socket: Socket,
  notifyDoors?: { os?: PluginOs; quiet?: CompletionNotifyQuiet; t?: PluginTranslate }
): () => void {
  rest = r
  os = notifyDoors?.os ?? null
  bindCompletionNotify(r, notifyDoors?.t, notifyDoors?.os, notifyDoors?.quiet)
  const unsubs: Array<() => void> = []

  queryClient.setQueryDefaults(KANBAN_KEY_ROOT, { enabled: routedToScope })
  unsubs.push(() => queryClient.setQueryDefaults(KANBAN_KEY_ROOT, {}))

  // Hydrate an atom from storage and keep storage in sync with it.
  const persist = <T>(atom: Persisted<T>, key: string, fallback: T) => {
    atom.set(storage.get(key, fallback))
    unsubs.push(atom.listen(value => storage.set(key, value)))
  }

  persist($introDismissed, INTRO_KEY, false)
  persist($lanesByProfile, LANES_KEY, false)
  persist($collapsedLanes, COLLAPSED_KEY, {})
  persist($gatewayFilter, GATEWAY_FILTER_KEY, '')

  eventCursorByBoard.clear()
  resetBoardRefresh()

  let close: (() => void) | null = null
  let socketGeneration = 0

  const dial = (scope: string, slug: string, since: number | undefined) => {
    const generation = socketGeneration
    const selectedSlug = $boardSlug.get()

    return socket(eventsUrl(slug, since), data => {
      if (generation === socketGeneration) {
        onEventsFrame(scope, slug, data, selectedSlug)
      }
    })
  }

  const open = (selectedSlug: string) => {
    const generation = ++socketGeneration
    const scope = kanbanConnectionScope()

    close?.()
    close = null

    const openResolved = (slug: string) => {
      if (generation !== socketGeneration) {
        return
      }

      const since = eventsSince(scope, slug)

      if (since !== undefined) {
        close = dial(scope, slug, since)

        return
      }

      // No cached tail yet. Wait for the snapshot and open at its
      // latest_event_id. A board switch or unload bumps the generation so a
      // late snapshot cannot open a stale socket. A failed fetch still opens
      // with no since — the server starts at the tail rather than replaying.
      void queryClient
        .fetchQuery({
          // [daveey] The same (merged, gateway-tagged) fetch the page runs —
          // a raw single-gateway snapshot under this key would briefly drop
          // every other gateway's cards.
          queryFn: () => (slug === $boardSlug.get() ? fetchBoard(false) : r<KanbanBoard>(boardSnapshotPath(slug))),
          queryKey: boardKey(scope, slug, false)
        })
        .then(board => {
          if (generation !== socketGeneration) {
            return
          }

          const tail = knownCursor(board?.latest_event_id)

          close = dial(scope, slug, tail)
        })
        .catch(() => {
          if (generation !== socketGeneration) {
            return
          }

          close = dial(scope, slug, undefined)
        })
    }

    if (selectedSlug) {
      openResolved(selectedSlug)

      return
    }

    // Resolve the alias BEFORE the handshake: /events is pinned to its board
    // when opened. Resolving on each frame could classify an old socket's
    // events against a different server-current board's cursor.
    void r<BoardsResponse>('/boards')
      .then(boards => openResolved(typeof boards.current === 'string' ? boards.current : ''))
      .catch(() => openResolved('')) // Keep live cache invalidation; notifications fail closed.
  }

  // The local connection keeps the BARE key (the bare-local rule of
  // lib/connection-scoped: byte-identical storage for single-backend users, and
  // the slug picked before per-connection keys existed survives the upgrade).
  // Remotes are suffixed by registry id. Pins follow the same rule: boards
  // belong to a gateway, so each connection keeps its own tabs.
  const scopedKey = (key: string) => {
    const scope = kanbanConnectionScope()

    return scope === LOCAL_SCOPE ? key : `${key}.${scope}`
  }

  const slugStorageKey = () => scopedKey(BOARD_SLUG_KEY)

  const restorePins = () => {
    const stored = storage.get<unknown>(scopedKey(PINNED_BOARDS_KEY), [])
    const slugs = Array.isArray(stored) ? stored.filter((s): s is string => typeof s === 'string' && !!s) : []

    $pinnedBoards.set([...new Set(slugs)])
  }

  restorePins()
  unsubs.push($pinnedBoards.listen(pins => storage.set(scopedKey(PINNED_BOARDS_KEY), pins)))
  $boardSlug.set(storage.get(slugStorageKey(), ''))
  unsubs.push($boardSlug.listen(slug => storage.set(slugStorageKey(), slug)))
  open($boardSlug.get())
  unsubs.push($boardSlug.listen(open))
  unsubs.push(
    host.state.connectionId.listen((next, prev) => {
      // Query keys embed the scope, so the new connection is already a cache
      // miss; only the LIVE bindings (socket, slug) follow it. The boot-time
      // null → 'local' publish is the same scope, not a switch. A changed slug
      // reopens the socket through the $boardSlug listener above; an unchanged
      // slug still needs a dial because the backend behind it changed.
      if ((next ?? LOCAL_SCOPE) === (prev ?? LOCAL_SCOPE)) {
        return
      }

      restorePins()
      const previous = $boardSlug.get()
      $boardSlug.set(storage.get(slugStorageKey(), ''))

      if ($boardSlug.get() === previous) {
        open(previous)
      }
    })
  )

  return () => {
    socketGeneration += 1
    eventCursorByBoard.clear()
    resetBoardRefresh()
    unsubs.forEach(unsub => unsub())
    close?.()
    rest = null
    os = null
  }
}

/** The plugin's OS door, for components too deep to be handed `ctx`. Null
 *  before `bindApi` and after unload. */
export const pluginOs = (): null | PluginOs => os

function call<T>(path: string, opts?: PluginRestOptions): Promise<T> {
  return rest ? rest<T>(path, opts) : Promise.reject(new Error('kanban api not ready'))
}

/** Append the selected board (and other params) to a path. */
function withBoard(path: string, params: Record<string, string> = {}): string {
  const search = new URLSearchParams(params)
  const slug = $boardSlug.get()

  if (slug) {
    search.set('board', slug)
  }

  const qs = search.toString()

  return qs ? `${path}?${qs}` : path
}

// ── query keys (connection- and board-scoped; scope is always segment [2]) ────

/** Prefix matching every board query on one connection (all slugs, both
 *  archived views) — the mutation-settled invalidation target. */
export const boardKeyPrefix = (scope: string) => ['kanban', 'board', scope] as const
export const boardKey = (scope: string, slug: string, archived: boolean) =>
  [...boardKeyPrefix(scope), slug, archived] as const

/** [daveey] Board poll interval: the socketless heartbeat, faster when the board spans gateways (no socket there). */
export const boardPollMs = (spansGateways: boolean) => (spansGateways ? 15_000 : 60_000)

/** [daveey] The board poll every board view runs (page, count pill), minus its queryFn (`() => fetchBoard(archived)`,
 *  added at the call site so test doubles of fetchBoard apply): one key, so one cache and one poll. Single-flight: an
 *  interval tick (and a new view mounting) while a read is in flight joins it rather than starting another, because
 *  neither passes cancelRefetch; socket refreshes wait for the read in flight (invalidateAfterInFlight).
 *  api.board-poll.test.ts pins that a board read slower than the interval does not stack polls. */
export const boardPoll = (scope: string, slug: string, archived: boolean, refetchInterval: number) => ({
  queryKey: boardKey(scope, slug, archived),
  refetchInterval
})
export const taskKey = (scope: string, slug: string, id: string, owner?: TaskOwner) =>
  owner
    ? (['kanban', 'task', owner.gateway, owner.board, id, owner.profile] as const)
    : (['kanban', 'task', scope, slug, id] as const)
export const logKey = (scope: string, slug: string, id: string, owner?: TaskOwner) =>
  owner
    ? (['kanban', 'log', owner.gateway, owner.board, id, owner.profile] as const)
    : (['kanban', 'log', scope, slug, id] as const)
export const boardsKey = (scope: string) => ['kanban', 'boards', scope] as const
/** [daveey] The routed gateway's server-current board slug (the '' alias), see resolvedSlug. */
const currentBoardKey = (scope: string) => ['kanban', 'current-board', scope] as const
const CURRENT_BOARD_STALE_MS = 30_000
export const profilesKey = (scope: string, owner?: TaskOwner) =>
  owner ? (['kanban', 'profiles', owner.gateway, owner.profile] as const) : (['kanban', 'profiles', scope] as const)
export const projectsKey = (scope: string) => ['kanban', 'projects', scope] as const
export const orchestrationKey = (scope: string) => ['kanban', 'orchestration', scope] as const
/** [daveey] Blocked cards across every board and gateway (sidebar section). */
export const blockedKey = (scope: string) => ['kanban', 'blocked', scope] as const

// ── gateways [daveey] ────────────────────────────────────────────────────────
//
// One board across EVERY registered gateway: the board read fans out to each
// connection's own kanban API and merges the columns, tagging every card with
// its owner. Each gateway keeps its own kanban.db and dispatcher — nothing is
// replicated — so every write about a task is routed back to the gateway that
// owns it (`taskOwners`, filled by each merged read). With one gateway (or a
// Desktop without the registry) every path below is the old single-gateway
// call, byte for byte.

/** Gateway filter: '' = every gateway, else one registry connection id. Persisted. */
export const $gatewayFilter = atom<string>('')

/** Remote gateways get a shorter leash than the active one: a dead ssh tunnel
 *  or a cloud gateway waiting on an OAuth login must not hold the whole board
 *  hostage. The bridge's own `timeoutMs` does not cover a pending login, so
 *  the deadline is enforced here too. */
const REMOTE_BOARD_TIMEOUT_MS = 6_000

/** After a remote gateway fails, skip it for this long (its error stays on
 *  the board) instead of making every poll wait out the deadline again. */
const REMOTE_COOLDOWN_MS = 60_000

const gatewayCooldown = new Map<string, { message: string; until: number }>()

/** Test seam: forget remembered gateway failures. */
export function resetGatewayCooldowns(): void {
  gatewayCooldown.clear()
}

/** One read from a remote gateway: hard deadline, failures cooled down. */
function remoteRead<T>(gateway: string, path: string): Promise<T> {
  const cooling = gatewayCooldown.get(gateway)

  if (cooling && cooling.until > Date.now()) {
    return Promise.reject(new Error(cooling.message))
  }

  return new Promise<T>((resolve, reject) => {
    const fail = (err: unknown) => {
      clearTimeout(timer)

      // A missing board is an answer, not an outage.
      if (!isMissingBoard(err)) {
        gatewayCooldown.set(gateway, {
          message: err instanceof Error ? err.message : String(err),
          until: Date.now() + REMOTE_COOLDOWN_MS
        })
      }

      reject(err)
    }

    const timer = setTimeout(
      () => fail(new Error(`no answer in ${REMOTE_BOARD_TIMEOUT_MS / 1000}s`)),
      REMOTE_BOARD_TIMEOUT_MS
    )

    call<T>(path, { ...via(gateway), timeoutMs: REMOTE_BOARD_TIMEOUT_MS }).then(value => {
      clearTimeout(timer)
      gatewayCooldown.delete(gateway)
      resolve(value)
    }, fail)
  })
}

export interface TaskOwner {
  readonly gateway: string
  readonly board: string
  readonly profile?: string
}

const taskOwners = new Map<string, { id: string; owner: TaskOwner }>()

function ownerKey(id: string, owner: TaskOwner): string {
  return JSON.stringify([owner.gateway, owner.profile ?? null, owner.board, id])
}

/** Legacy callers are safe only when the id has exactly one known owner. */
function rememberedOwner(id: string): TaskOwner | undefined {
  const matches = [...taskOwners.values()].filter(entry => entry.id === id)

  if (matches.length > 1) {
    throw new Error(`Ambiguous task id ${id}: supply gateway, profile and board`)
  }

  return matches[0]?.owner
}

/** [daveey] Cards read from the hermes-hub replica, by qualified owner key: their hub gateway
 *  name and board, so a comment on one becomes a hub request instead of a
 *  call to a gateway that just failed to answer. */
const hubTaskRefs = new Map<string, { board: string; hubGateway: string }>()

/** [daveey] Record where a task lives before the board has read it (the
 *  open-task hook: a card opened from a chat on another gateway), so the
 *  drawer's first fetch goes to the owning gateway + board. */
export function rememberTaskOwner(id: string, board: string, gateway: string, profile?: string): void {
  const owner = Object.freeze({ board, gateway, ...(profile === undefined ? {} : { profile }) })
  taskOwners.set(ownerKey(id, owner), { id, owner })
}

/** Registry ids of every gateway, the routed (active) one first. A single
 *  gateway, or a legacy profile-only Desktop, yields just the routed scope. */
export function boardGateways(): string[] {
  const active = routedScope()
  // Optional chain: a host SDK older than the gateways atom reads as one gateway.
  const ids = (host.state.gateways?.get() ?? []).map(gateway => gateway.id)

  if (ids.length < 2) {
    return [active]
  }

  return [active, ...ids.filter(id => id !== active)]
}

export const multiGateway = (): boolean => boardGateways().length > 1

/** REST options that send a call to `gateway` — none for the routed one, so
 *  its calls keep the ambient profile exactly as before. */
function via(gateway: string | undefined): PluginRestOptions {
  return gateway && gateway !== routedScope() ? { connectionId: gateway } : {}
}

/** Owning gateway of a task (the routed one when unknown — e.g. a task id
 *  typed before the board loaded, or a single-gateway setup). */
export function taskGateway(id: string): string {
  return rememberedOwner(id)?.gateway ?? routedScope()
}

function withSlug(path: string, slug: string, params: Record<string, string> = {}): string {
  const search = new URLSearchParams(params)

  if (slug) {
    search.set('board', slug)
  }

  const qs = search.toString()

  return qs ? `${path}?${qs}` : path
}

/** `withBoard` for a task: its own board slug on its own gateway. */
function forTask(
  id: string,
  path: string,
  params: Record<string, string> = {},
  explicitOwner?: TaskOwner
): [string, PluginRestOptions] {
  const owner = explicitOwner ?? rememberedOwner(id)

  return owner
    ? [
        withSlug(path, owner.board, params),
        owner.profile === undefined ? via(owner.gateway) : { connectionId: owner.gateway, profile: owner.profile }
      ]
    : [withBoard(path, params), {}]
}

/** The selected slug, with the '' (server-current) alias resolved against the
 *  ROUTED gateway — remote gateways must show that same board, not their own
 *  current one. */
async function resolvedSlug(): Promise<string> {
  const slug = $boardSlug.get()

  if (slug) {
    return slug
  }

  // [daveey] Every board refetch (socket frame, poll, retry) lands here, so the
  // answer is cached per routed scope for CURRENT_BOARD_STALE_MS and concurrent
  // callers share one in-flight read. Re-reading the full `/boards` list (per-board
  // counts) before each board read doubled the routed gateway's traffic: 297 `/boards`
  // timeouts in 30 min on a slow link, 2026-11-13. A failed read keeps the last answer.
  const queryKey = currentBoardKey(routedScope())

  try {
    return await queryClient.fetchQuery({
      queryFn: async () => {
        const boards = await call<BoardsResponse>('/boards')

        return typeof boards.current === 'string' ? boards.current : ''
      },
      queryKey,
      retry: false,
      staleTime: CURRENT_BOARD_STALE_MS
    })
  } catch {
    return queryClient.getQueryData<string>(queryKey) ?? ''
  }
}

const isMissingBoard = (err: unknown): boolean => {
  const message = err instanceof Error ? err.message : String(err)

  return /^\s*404\b/.test(message) || /does not exist|is archived/.test(message)
}

export interface GatewayBoardPart {
  gateway: string
  board?: KanbanBoard
  error?: unknown
  /** [daveey] Set when `board` is the hermes-hub replica's copy (the live
   *  read failed): epoch seconds it is current for, or null when unknown. */
  hubSyncedAt?: null | number
}

const TERMINAL_ORDER = new Set(['done', 'archived'])

/** Merge per-gateway boards into one: columns in first-seen order, every task
 *  tagged with its `gateway`, queue lanes re-sorted by the backend's dispatch
 *  key (priority DESC, created_at ASC) and history lanes newest-first. The
 *  FIRST part is the routed gateway: its `latest_event_id`/`now` drive the
 *  events socket; when it did not answer, `latest_event_id` is 0 = unknown
 *  (never a cursor: see knownCursor). A gateway that lacks the board contributes nothing; any
 *  other failure is reported in `gateway_errors`. Throws only when no
 *  gateway answered. */
export function mergeBoards(parts: GatewayBoardPart[]): KanbanBoard {
  const answered = parts.filter(part => part.board)

  if (answered.length === 0) {
    const failed = parts.find(part => part.error && !isMissingBoard(part.error)) ?? parts[0]

    throw failed?.error ?? new Error('kanban board unavailable')
  }

  const columns = new Map<string, KanbanColumn>()
  const tenants = new Set<string>()
  const assignees = new Set<string>()
  const gatewayErrors: Record<string, string> = {}
  const hubGateways: Record<string, null | number> = {}

  for (const part of parts) {
    // A replica-filled part still failed live: keep its error on the board
    // (the header chip says the gateway is down) next to its hub cards.
    if (part.error && !isMissingBoard(part.error)) {
      gatewayErrors[part.gateway] = part.error instanceof Error ? part.error.message : String(part.error)
    }

    if (!part.board) {
      continue
    }

    if (part.hubSyncedAt !== undefined) {
      hubGateways[part.gateway] = part.hubSyncedAt
    }

    for (const col of part.board.columns ?? []) {
      const merged = columns.get(col.name) ?? { name: col.name, tasks: [] }
      merged.tasks.push(...col.tasks.map(task => ({ ...task, gateway: part.gateway })))
      columns.set(col.name, merged)
    }

    part.board.tenants?.forEach(name => tenants.add(name))
    part.board.assignees?.forEach(name => assignees.add(name))
  }

  if (answered.length > 1) {
    for (const col of columns.values()) {
      if (TERMINAL_ORDER.has(col.name)) {
        col.tasks.sort(
          (a, b) =>
            Number(a.completed_at == null) - Number(b.completed_at == null) ||
            (b.completed_at ?? 0) - (a.completed_at ?? 0) ||
            (b.created_at ?? 0) - (a.created_at ?? 0)
        )
      } else {
        col.tasks.sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0) || (a.created_at ?? 0) - (b.created_at ?? 0))
      }
    }
  }

  const primary = parts[0]?.board

  return {
    assignees: [...assignees].sort(),
    columns: [...columns.values()],
    gateway_errors: Object.keys(gatewayErrors).length ? gatewayErrors : undefined,
    ...(Object.keys(hubGateways).length ? { hub_gateways: hubGateways } : {}),
    latest_event_id: primary?.latest_event_id ?? 0,
    now: primary?.now ?? answered[0].board!.now,
    tenants: [...tenants].sort()
  }
}

function rememberOwners(board: KanbanBoard, slug: string): KanbanBoard {
  for (const col of board.columns) {
    for (const task of col.tasks) {
      if (task.gateway) {
        rememberTaskOwner(task.id, slug, task.gateway)
      }

      if (task.from_hub && task.gateway) {
        const hubGateway = hubGatewayForConnection(task.gateway)

        if (hubGateway) {
          hubTaskRefs.set(ownerKey(task.id, { board: slug, gateway: task.gateway }), {
            board: slug || 'default',
            hubGateway
          })
        }
      } else {
        hubTaskRefs.delete(ownerKey(task.id, { board: slug, gateway: task.gateway ?? routedScope() }))
      }
    }
  }

  return board
}

// ── hermes-hub replica fallback [daveey] ──────────────────────────────────────
//
// A registered gateway whose live board read fails (asleep, tunnel down,
// waiting on a login) still shows its cards: the window's own gateway serves
// its hermes-hub replica (`/api/plugins/hub-sync/tasks`), and those cards carry
// `from_hub` so the board marks them. The live read stays the primary source;
// the replica is only consulted for gateways that didn't answer.

const HUB_COLUMN_ORDER = ['triage', 'todo', 'scheduled', 'ready', 'running', 'blocked', 'review', 'done', 'archived']

/** One gateway's replica cards as a board (columns in the board's lane order). */
export function hubBoardFromTasks(tasks: HubTask[], now: number): KanbanBoard {
  const columns = new Map<string, KanbanColumn>()
  const assignees = new Set<string>()

  for (const task of tasks) {
    const status = task.status || 'todo'
    const run = task.run ?? null
    const column = columns.get(status) ?? { name: status, tasks: [] }

    column.tasks.push({
      assignee: task.assignee ?? null,
      body: task.body ?? null,
      comment_count: typeof task.comments === 'number' ? task.comments : undefined,
      completed_at: task.completed_at ?? null,
      created_at: task.created_at,
      current_run_started_at: run?.status === 'running' ? (run.started_at ?? null) : null,
      from_hub: true,
      hub_synced_at: task.synced_at,
      id: task.id,
      latest_summary: run?.summary ?? null,
      priority: task.priority,
      started_at: task.started_at ?? null,
      status,
      title: task.title ?? task.id
    })
    columns.set(status, column)

    if (task.assignee) {
      assignees.add(task.assignee)
    }
  }

  const rank = (name: string) => {
    const at = HUB_COLUMN_ORDER.indexOf(name)

    return at < 0 ? HUB_COLUMN_ORDER.length : at
  }

  return {
    assignees: [...assignees].sort(),
    columns: [...columns.values()].sort((a, b) => rank(a.name) - rank(b.name)),
    latest_event_id: 0,
    now,
    tenants: []
  }
}

/** Fill failed remote parts from the replica (in place). Parts that answered,
 *  a missing board, and gateways with no known hub name are left alone. */
export async function withHubFallback(
  parts: GatewayBoardPart[],
  slug: string,
  archived: boolean
): Promise<GatewayBoardPart[]> {
  await Promise.all(
    parts.map(async (part, index) => {
      if (index === 0 || part.board || !part.error || isMissingBoard(part.error)) {
        return
      }

      const hubGateway = hubGatewayForConnection(part.gateway)

      if (!hubGateway) {
        return
      }

      const tasks = await hubGatewayTasks(hubGateway, slug || 'default', archived)

      if (!tasks) {
        return
      }

      const synced = tasks.map(task => task.synced_at).filter((at): at is number => typeof at === 'number')

      part.board = hubBoardFromTasks(tasks, Math.floor(Date.now() / 1000))
      part.hubSyncedAt = synced.length ? Math.min(...synced) : null
    })
  )

  return parts
}

// ── reads ─────────────────────────────────────────────────────────────────────

export async function fetchBoard(archived: boolean): Promise<KanbanBoard> {
  const params: Record<string, string> = archived ? { include_archived: 'true' } : {}
  const gateways = boardGateways()

  if (gateways.length < 2) {
    return call<KanbanBoard>(withBoard('/board', params))
  }

  const slug = await resolvedSlug()

  const settled = await Promise.allSettled(
    gateways.map((gateway, index) =>
      index === 0
        ? call<KanbanBoard>(withBoard('/board', params))
        : remoteRead<KanbanBoard>(gateway, withSlug('/board', slug, params))
    )
  )

  const parts: GatewayBoardPart[] = settled.map((result, index) => ({
    gateway: gateways[index],
    ...(result.status === 'fulfilled' ? { board: result.value } : { error: result.reason })
  }))

  // [daveey] Each gateway that answered says which hub gateway it is (cached
  // for minutes), so its cards can come from the replica when it is down later.
  parts.forEach(part => part.board && void learnHubGatewayName(part.gateway))

  const board = mergeBoards(await withHubFallback(parts, slug, archived))

  return rememberOwners(board, slug)
}

/** [daveey] One opening of a task detail view. Its reads of each kind (`kind` names the query) go out as priority
 *  'foreground' until that kind's open read has settled, so a slow link's poll thinner never skips the user's read:
 *  not on a re-open within the thinning gap, and not on React Query's retry of a failed first read (`retry`: this
 *  call retries a failed read of the same fetch). The open read settles when a read answers, or when a fetch whose
 *  reads all failed has spent its retries: the next fetch then starts with `retry` false. A fetch that a socket
 *  invalidation (cancelRefetch) cancels in a failed read's retry delay settles it too, by design: the user's read
 *  already went out foreground and failed, and the replacement fetch starts with `retry` false. Every later read of that
 *  kind (interval or socket refetch) is a background poll again and may be thinned (electron/slow-link.ts), so a kind
 *  whose reads keep failing (gateway unreachable, task gone, timeouts on a very slow link) stops bypassing the
 *  thinner after its first fetch. */
export function userOpenReads(): <T>(
  kind: string,
  read: (options: PluginRestOptions) => Promise<T>,
  retry?: boolean
) => Promise<T> {
  const delivered = new Set<string>()
  const failed = new Set<string>()

  return async (kind, read, retry = false) => {
    if (failed.has(kind) && !retry) {
      delivered.add(kind)
    }

    try {
      const result = await read(delivered.has(kind) ? {} : { priority: 'foreground' })

      delivered.add(kind)

      return result
    } catch (error) {
      failed.add(kind)

      throw error
    }
  }
}

/** [daveey] What `retryingFetch` reads of a React Query function context. */
export type ReadFetchContext = { client: typeof queryClient; queryKey: readonly unknown[] }

/** [daveey] Whether React Query is calling a query function to retry a failed read of the same fetch: it counts the
 *  fetch's failures before each retry and resets the count when a new fetch starts. That leans on query-core reducer
 *  details it does not document ('failed' sets `fetchFailureCount`, 'fetch' and 'success' reset it), including a fetch
 *  that `cancelRefetch` (a socket invalidation) starts in a failed fetch's retry delay: query-core 5.104 still
 *  dispatches 'fetch' there, because the restart's undefined meta differs from the running fetch's `fetchMeta` (null).
 *  drawer.test.tsx pins both; after a React Query bump, check "makes the refetches of a kind whose reads all fail polls
 *  once its first fetch spent its retries" and "makes the reads of a fetch that replaced a failed one during its retry
 *  delay polls". */
export const retryingFetch = ({ client, queryKey }: ReadFetchContext) =>
  (client.getQueryState(queryKey)?.fetchFailureCount ?? 0) > 0

export const fetchTask = async (id: string, owner?: TaskOwner, read: PluginRestOptions = {}) => {
  const downloadAttachment = captureGatewayFileDownload(
    owner ? { connectionId: owner.gateway, profile: owner.profile } : undefined
  )

  const [path, opts] = forTask(id, `/tasks/${id}`, {}, owner)
  const detail = await call<KanbanTaskDetail>(path, { ...opts, ...read })

  return { ...detail, downloadAttachment }
}

/** Worker stdout/stderr tail (last 16 KiB — plenty for the drawer). */
export const fetchLog = (id: string, owner?: TaskOwner, read: PluginRestOptions = {}) => {
  const [path, opts] = forTask(id, `/tasks/${id}/log`, { tail: '16384' }, owner)

  return call<WorkerLog>(path, { ...opts, ...read })
}

/** Every gateway's boards as one list: union by slug, card totals summed,
 *  `current` = the routed gateway's. A remote-only board is listed too, so
 *  picking it shows its cards. */
export async function fetchBoards(): Promise<BoardsResponse> {
  const gateways = boardGateways()

  if (gateways.length < 2) {
    return call<BoardsResponse>('/boards')
  }

  const settled = await Promise.allSettled(
    gateways.map((gateway, index) =>
      index === 0 ? call<BoardsResponse>('/boards') : remoteRead<BoardsResponse>(gateway, '/boards')
    )
  )

  const primary = settled[0]

  if (primary.status === 'rejected' && settled.every(result => result.status === 'rejected')) {
    throw primary.reason
  }

  return mergeBoardLists(
    settled.map((result, index) => ({
      gateway: gateways[index],
      boards: result.status === 'fulfilled' ? result.value : null
    }))
  )
}

export function mergeBoardLists(parts: Array<{ gateway: string; boards: BoardsResponse | null }>): BoardsResponse {
  const bySlug = new Map<string, BoardMeta>()
  const current = parts[0]?.boards?.current ?? parts.find(part => part.boards)?.boards?.current ?? 'default'

  for (const part of parts) {
    for (const meta of part.boards?.boards ?? []) {
      const seen = bySlug.get(meta.slug)

      if (!seen) {
        bySlug.set(meta.slug, { ...meta, gateways: [part.gateway], is_current: meta.slug === current })

        continue
      }

      seen.total = (seen.total ?? 0) + (meta.total ?? 0)
      seen.gateways = [...(seen.gateways ?? []), part.gateway]
    }
  }

  return { boards: [...bySlug.values()], current }
}

// ── [daveey] blocked cards across every board (the sidebar's Blocked section) ─

/** A blocked card plus the board it lives on (the drawer opens it there). */
export interface BlockedTask extends KanbanTask {
  board: string
  /** The chat managing the card, resolved to its live compression tip and
   *  title on the card's own gateway; null when no session owns it. */
  manager: null | { id: string; profile: null | string; title: null | string }
}

interface SessionLabels {
  /** The answering backend's Hermes profile (where these sessions live). */
  profile?: string
  sessions?: Record<string, { tip_id?: string; title?: null | string }>
}

interface BoardsWithCounts extends BoardsResponse {
  boards: Array<BoardMeta & { counts?: Record<string, number> }>
}

const managerId = (task: KanbanTask): null | string =>
  (task.managing_session_id ?? task.session_id ?? '').trim() || null

/** Every `blocked` card on every board of every gateway, highest priority
 *  first, then newest. Boards whose `/boards` counts say they hold no blocked
 *  card are skipped, so an idle fleet costs one `/boards` read per gateway.
 *  A gateway that fails contributes nothing; throws only when all fail. */
export async function fetchBlockedTasks(): Promise<BlockedTask[]> {
  const gateways = boardGateways()

  const perGateway = await Promise.allSettled(
    gateways.map(async (gateway, index) => {
      const read = <T>(path: string) => (index === 0 ? call<T>(path) : remoteRead<T>(gateway, path))
      const { boards } = await read<BoardsWithCounts>('/boards')

      const slugs = (boards ?? [])
        .filter(meta => (meta.counts ? (meta.counts.blocked ?? 0) > 0 : true))
        .map(meta => meta.slug)

      const parts = await Promise.allSettled(
        slugs.map(async slug => {
          const board = await read<KanbanBoard>(withSlug('/board', slug))
          const tasks = board.columns.find(col => col.name === 'blocked')?.tasks ?? []

          return tasks.map(task => ({
            ...task,
            board: slug,
            gateway: gateways.length > 1 ? gateway : task.gateway,
            manager: null
          }))
        })
      )

      const found = parts.flatMap(part => (part.status === 'fulfilled' ? part.value : []))
      const ids = [...new Set(found.map(managerId).filter((id): id is string => Boolean(id)))]
      let labels: SessionLabels['sessions'] = {}
      let profile: null | string = null

      if (ids.length) {
        try {
          const answer = await read<SessionLabels>(`/session-labels?ids=${encodeURIComponent(ids.join(','))}`)
          labels = answer.sessions ?? {}
          profile = answer.profile?.trim() || null
        } catch {
          // Older backend without the endpoint: group by the raw id, untitled.
        }
      }

      return found.map((task): BlockedTask => {
        const id = managerId(task)
        const label = id ? labels?.[id] : undefined

        return {
          ...task,
          gateway: task.gateway ?? (gateways.length > 1 ? gateway : undefined),
          manager: id ? { id: label?.tip_id || id, profile, title: label?.title?.trim() || null } : null
        }
      })
    })
  )

  if (perGateway.every(result => result.status === 'rejected')) {
    throw (perGateway[0] as PromiseRejectedResult).reason
  }

  const tasks = perGateway.flatMap(result => (result.status === 'fulfilled' ? result.value : []))

  for (const task of tasks) {
    rememberTaskOwner(task.id, task.board, task.gateway ?? gateways[0])
  }

  return tasks.sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0) || (b.created_at ?? 0) - (a.created_at ?? 0))
}

/** The assignee roster of `gateway` (default: the routed one) — profiles are
 *  per gateway, so a create aimed at another gateway lists ITS profiles. */
export const fetchProfiles = () => call<{ profiles: KanbanProfile[] }>('/profiles')

export const fetchOwnerProfiles = (owner: TaskOwner) =>
  call<{ profiles: KanbanProfile[] }>('/profiles', { connectionId: owner.gateway, profile: owner.profile })

export const fetchGatewayProfiles = (gateway: string) => remoteRead<{ profiles: KanbanProfile[] }>(gateway, '/profiles')

/** First-class Hermes projects, for scoping a board's default workspace. */
export const fetchProjects = () => call<{ projects: KanbanProject[] }>('/projects')

export const fetchOrchestration = () => call<OrchestrationSettings>('/orchestration')

// ── writes ────────────────────────────────────────────────────────────────────

// Every board edit nudges the dispatcher (debounced, fire-and-forget) so the
// change takes effect NOW instead of on the next 60s tick — create a ready
// task and the worker spawns immediately, no manual "nudge" ritual. The tick
// is lock-guarded and ~1ms when there's nothing to do, so over-nudging is
// free; failures are non-events (the periodic tick still exists). [daveey]
// The nudge goes to the gateway the write landed on — one timer per gateway.
const nudgeTimers = new Map<string, ReturnType<typeof setTimeout>>()

function autoNudge(gateway: string | TaskOwner = routedScope()): void {
  const key = typeof gateway === 'string' ? gateway : ownerKey('', gateway)
  const pending = nudgeTimers.get(key)

  if (pending != null) {
    clearTimeout(pending)
  }

  nudgeTimers.set(
    key,
    setTimeout(() => {
      nudgeTimers.delete(key)

      const request =
        typeof gateway === 'string'
          ? nudgeDispatcher(gateway)
          : call(withSlug('/dispatch', gateway.board), {
              connectionId: gateway.gateway,
              profile: gateway.profile,
              method: 'POST',
              body: {}
            })

      request.catch(() => undefined)
    }, 400)
  )
}

/** Resolve the write, then kick the owning gateway's dispatcher. Rejections pass through. */
function nudged<T>(write: Promise<T>, gateway?: string | TaskOwner): Promise<T> {
  const captured = typeof gateway === 'object' ? { ...gateway } : gateway

  return write.then(value => {
    autoNudge(captured)

    return value
  })
}

export const patchTask = (id: string, patch: Record<string, unknown>, owner?: TaskOwner) => {
  const [path, opts] = forTask(id, `/tasks/${id}`, {}, owner)

  return nudged(call(path, { ...opts, method: 'PATCH', body: patch }), owner ?? taskGateway(id))
}

/** Create on `gateway` (default: the routed one). The board slug is the
 *  selected one, resolved — a remote gateway must not fall back to its own
 *  current board. */
export const createTask = async (body: Record<string, unknown>, gateway?: string) => {
  if (!gateway || gateway === routedScope()) {
    return nudged(call<{ task: KanbanTask | null; warning?: string }>(withBoard('/tasks'), { method: 'POST', body }))
  }

  const slug = await resolvedSlug()

  const created = await nudged(
    call<{ task: KanbanTask | null; warning?: string }>(withSlug('/tasks', slug), {
      ...via(gateway),
      method: 'POST',
      body
    }),
    gateway
  )

  // Route the follow-up writes (the dialog's lane move) before any board read
  // has seen the new card.
  if (created.task?.id) {
    rememberTaskOwner(created.task.id, slug, gateway)
  }

  return created
}

// Deleting can unblock dependants (a gone parent no longer gates), so it
// nudges too.
export const deleteTask = (id: string, owner?: TaskOwner) => {
  const [path, opts] = forTask(id, `/tasks/${id}`, {}, owner)

  return nudged(call(path, { ...opts, method: 'DELETE' }), owner ?? taskGateway(id))
}

type BulkResult = { results: Array<{ id: string; ok: boolean; error?: string }> }

/** One patch, many ids — independent per-id application; returns per-id
 *  outcomes so the UI can toast partial failures. [daveey] Split per owning
 *  gateway (and board); a whole gateway failing reports each of its ids. */
export async function bulkTasks(ids: string[], patch: Record<string, unknown>): Promise<BulkResult> {
  const groups = new Map<string, { ids: string[]; path: string; opts: PluginRestOptions; gateway: string }>()

  for (const id of ids) {
    // Capture the complete route before starting any asynchronous writes.
    const [path, opts] = forTask(id, '/tasks/bulk')
    const gateway = taskGateway(id)
    const key = JSON.stringify([gateway, opts.profile, path])
    const group = groups.get(key) ?? { ids: [], path, opts, gateway }
    group.ids.push(id)
    groups.set(key, group)
  }

  const outcomes = await Promise.all(
    [...groups.values()].map(async ({ ids: group, path, opts, gateway }) => {
      try {
        return await nudged(
          call<BulkResult>(path, { ...opts, method: 'POST', body: { ids: group, ...patch } }),
          gateway
        )
      } catch (err) {
        if (groups.size === 1) {
          throw err
        }

        const error = err instanceof Error ? err.message : String(err)

        return { results: group.map(id => ({ error, id, ok: false })) }
      }
    })
  )

  return { results: outcomes.flatMap(outcome => outcome.results) }
}

export const addComment = (id: string, body: string, owner?: TaskOwner) => {
  const captured = owner ?? rememberedOwner(id)
  // The legacy replica request protocol has no profile field. Never queue a
  // qualified-profile write through that lossy route.
  const hubRef = captured && captured.profile === undefined ? hubTaskRefs.get(ownerKey(id, captured)) : undefined

  // [daveey] A replica card's owner is unreachable: queue the comment as a
  // hermes-hub owner request (applied when that gateway syncs again).
  if (hubRef) {
    return hubRequest({
      action: 'comment',
      args: { text: body },
      coll: 'tasks',
      gateway: hubRef.hubGateway,
      key: `${hubRef.board}/${id}`
    })
  }

  const [path, opts] = forTask(id, `/tasks/${id}/comments`, {}, captured)

  return call(path, { ...opts, method: 'POST', body: { author: 'desktop', body } })
}

export const reassignTask = (id: string, profile: string, owner?: TaskOwner) => {
  const [path, opts] = forTask(id, `/tasks/${id}/reassign`, {}, owner)

  return nudged(
    call(path, { ...opts, method: 'POST', body: { profile, reclaim_first: true } }),
    owner ?? taskGateway(id)
  )
}

export const reclaimTask = (id: string, owner?: TaskOwner) => {
  const [path, opts] = forTask(id, `/tasks/${id}/reclaim`, {}, owner)

  return nudged(call(path, { ...opts, method: 'POST', body: {} }), owner ?? taskGateway(id))
}

export const uploadAttachment = (
  id: string,
  upload: { filename: string; contentType?: string; bytes: ArrayBuffer },
  owner?: TaskOwner
) => {
  const [path, opts] = forTask(id, `/tasks/${id}/attachments`, {}, owner)

  return call(path, { ...opts, method: 'POST', upload })
}

export const createBoard = (slug: string, name: string, projectId?: string) =>
  call<{ board: { slug: string } }>('/boards', {
    method: 'POST',
    body: { slug, name, ...(projectId ? { project_id: projectId } : {}) }
  })

/** Rough auxiliary-model estimate for a task (tokens + complexity). Makes a
 *  model call — gate behind an explicit user action + disclaimer. */
export const estimateTask = (id: string, owner?: TaskOwner) => {
  const [path, opts] = forTask(id, `/tasks/${id}/estimate`, {}, owner)

  return call<TaskEstimate>(path, { ...opts, method: 'POST', body: {} })
}

/** Estimate from typed title/body before a task exists (create dialog). */
export const estimateNew = (title: string, body: string) =>
  call<TaskEstimate>('/estimate', { method: 'POST', body: { title, body: body || undefined } })

/** Edit a board's display metadata + default project directory. Pass
 *  `default_workdir: ''` to clear it. Slug is immutable. */
export const updateBoard = (slug: string, patch: Record<string, unknown>) =>
  call<{ board: BoardMeta }>(`/boards/${encodeURIComponent(slug)}`, { method: 'PATCH', body: patch })

/** Archive a board to `boards/_archived/` — recoverable, and the backend
 *  refuses to touch `default`. (`?delete=true` hard-deletes; no caller yet.) */
export const deleteBoard = (slug: string) =>
  call<{ result: { action: string; new_path: string }; current: string }>(`/boards/${encodeURIComponent(slug)}`, {
    method: 'DELETE'
  })

// Board transfer exchanges filesystem paths, not bytes — the picker runs on
// the machine hosting the backend, so the backend reads and writes the file.

export const exportBoard = (slug: string, output: string) =>
  call<BoardExportResult>(`/boards/${encodeURIComponent(slug)}/export`, { method: 'POST', body: { output } })

export const importBoard = (archive: string) =>
  call<BoardImportResult>('/boards/import', { method: 'POST', body: { archive } })

export const nudgeDispatcher = (gateway?: string) =>
  !gateway || gateway === routedScope()
    ? call<{ spawned?: unknown[] }>(withBoard('/dispatch'), { method: 'POST', body: {} })
    : resolvedSlug().then(slug =>
        call<{ spawned?: unknown[] }>(withSlug('/dispatch', slug), { ...via(gateway), method: 'POST', body: {} })
      )

export const saveOrchestration = (patch: Record<string, unknown>) =>
  call<OrchestrationSettings>('/orchestration', { method: 'PUT', body: patch })

export const saveProfileDescription = (name: string, description: string) =>
  call(`/profiles/${encodeURIComponent(name)}`, { method: 'PATCH', body: { description } })

export const autoDescribeProfile = (name: string) =>
  call<{ ok: boolean; reason?: null | string; description?: null | string }>(
    `/profiles/${encodeURIComponent(name)}/describe-auto`,
    { method: 'POST', body: { overwrite: true } }
  )
