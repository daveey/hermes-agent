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

function snapshotCursor(scope: string, slug: string): number | undefined {
  for (const archived of [false, true]) {
    const board = queryClient.getQueryData<KanbanBoard>(boardKey(scope, slug, archived))

    if (typeof board?.latest_event_id === 'number') {
      return board.latest_event_id
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

/** One live `task_events` frame → precise cache invalidation: the board, plus
 *  each touched task's detail. The polls (8s board / 4s drawer) stay as the
 *  fallback — the socket just makes the board feel instant. */
function onEventsFrame(scope: string, slug: string, data: unknown, selectedSlug = slug): void {
  const frame = data as { cursor?: unknown; events?: CompletionEvent[] }

  if (typeof frame?.cursor === 'number') {
    eventCursorByBoard.set(cursorKey(scope, slug), frame.cursor)
  }

  const events = frame?.events

  if (!events?.length) {
    return
  }

  void queryClient.invalidateQueries({ queryKey: boardKeyPrefix(scope) })
  // Any event can change a board's card count — keep the switcher badge honest.
  void queryClient.invalidateQueries({ queryKey: boardsKey(scope) })
  // [daveey] The sidebar's Blocked section spans every board.
  void queryClient.invalidateQueries({ queryKey: blockedKey(scope) })

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

          const tail = typeof board?.latest_event_id === 'number' ? board.latest_event_id : undefined

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
  // Remotes are suffixed by registry id.
  const slugStorageKey = () => {
    const scope = kanbanConnectionScope()

    return scope === LOCAL_SCOPE ? BOARD_SLUG_KEY : `${BOARD_SLUG_KEY}.${scope}`
  }

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
export const taskKey = (scope: string, slug: string, id: string) => ['kanban', 'task', scope, slug, id] as const
export const logKey = (scope: string, slug: string, id: string) => ['kanban', 'log', scope, slug, id] as const
export const boardsKey = (scope: string) => ['kanban', 'boards', scope] as const
export const profilesKey = (scope: string) => ['kanban', 'profiles', scope] as const
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

interface TaskOwner {
  gateway: string
  board: string
}

const taskOwners = new Map<string, TaskOwner>()

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
  return taskOwners.get(id)?.gateway ?? routedScope()
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
function forTask(id: string, path: string, params: Record<string, string> = {}): [string, PluginRestOptions] {
  const owner = taskOwners.get(id)

  return owner && owner.gateway !== routedScope()
    ? [withSlug(path, owner.board, params), via(owner.gateway)]
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

  try {
    const boards = await call<BoardsResponse>('/boards')

    return typeof boards.current === 'string' ? boards.current : ''
  } catch {
    return ''
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
}

const TERMINAL_ORDER = new Set(['done', 'archived'])

/** Merge per-gateway boards into one: columns in first-seen order, every task
 *  tagged with its `gateway`, queue lanes re-sorted by the backend's dispatch
 *  key (priority DESC, created_at ASC) and history lanes newest-first. The
 *  FIRST part is the routed gateway: its `latest_event_id`/`now` drive the
 *  events socket. A gateway that lacks the board contributes nothing; any
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

  for (const part of parts) {
    if (!part.board) {
      if (part.error && !isMissingBoard(part.error)) {
        gatewayErrors[part.gateway] = part.error instanceof Error ? part.error.message : String(part.error)
      }

      continue
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
    latest_event_id: primary?.latest_event_id ?? 0,
    now: primary?.now ?? answered[0].board!.now,
    tenants: [...tenants].sort()
  }
}

function rememberOwners(board: KanbanBoard, slug: string): KanbanBoard {
  for (const col of board.columns) {
    for (const task of col.tasks) {
      if (task.gateway) {
        taskOwners.set(task.id, { board: slug, gateway: task.gateway })
      }
    }
  }

  return board
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

  const board = mergeBoards(
    settled.map((result, index) => ({
      gateway: gateways[index],
      ...(result.status === 'fulfilled' ? { board: result.value } : { error: result.reason })
    }))
  )

  return rememberOwners(board, slug)
}

export const fetchTask = async (id: string) => {
  const downloadAttachment = captureGatewayFileDownload()
  const detail = await call<KanbanTaskDetail>(...forTask(id, `/tasks/${id}`))

  return { ...detail, downloadAttachment }
}

/** Worker stdout/stderr tail (last 16 KiB — plenty for the drawer). */
export const fetchLog = (id: string) => call<WorkerLog>(...forTask(id, `/tasks/${id}/log`, { tail: '16384' }))

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
  manager: null | { id: string; title: null | string }
}

interface SessionLabels {
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

      if (ids.length) {
        try {
          labels = (await read<SessionLabels>(`/session-labels?ids=${encodeURIComponent(ids.join(','))}`)).sessions ?? {}
        } catch {
          // Older backend without the endpoint: group by the raw id, untitled.
        }
      }

      return found.map((task): BlockedTask => {
        const id = managerId(task)
        const label = id ? labels?.[id] : undefined

        return { ...task, manager: id ? { id: label?.tip_id || id, title: label?.title?.trim() || null } : null }
      })
    })
  )

  if (perGateway.every(result => result.status === 'rejected')) {
    throw (perGateway[0] as PromiseRejectedResult).reason
  }

  const tasks = perGateway.flatMap(result => (result.status === 'fulfilled' ? result.value : []))

  for (const task of tasks) {
    taskOwners.set(task.id, { board: task.board, gateway: task.gateway ?? gateways[0] })
  }

  return tasks.sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0) || (b.created_at ?? 0) - (a.created_at ?? 0))
}

/** The assignee roster of `gateway` (default: the routed one) — profiles are
 *  per gateway, so a create aimed at another gateway lists ITS profiles. */
export const fetchProfiles = () => call<{ profiles: KanbanProfile[] }>('/profiles')

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

function autoNudge(gateway: string = routedScope()): void {
  const pending = nudgeTimers.get(gateway)

  if (pending != null) {
    clearTimeout(pending)
  }

  nudgeTimers.set(
    gateway,
    setTimeout(() => {
      nudgeTimers.delete(gateway)
      nudgeDispatcher(gateway).catch(() => undefined)
    }, 400)
  )
}

/** Resolve the write, then kick the owning gateway's dispatcher. Rejections pass through. */
function nudged<T>(write: Promise<T>, gateway?: string): Promise<T> {
  return write.then(value => {
    autoNudge(gateway)

    return value
  })
}

export const patchTask = (id: string, patch: Record<string, unknown>) => {
  const [path, opts] = forTask(id, `/tasks/${id}`)

  return nudged(call(path, { ...opts, method: 'PATCH', body: patch }), taskGateway(id))
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
    taskOwners.set(created.task.id, { board: slug, gateway })
  }

  return created
}

// Deleting can unblock dependants (a gone parent no longer gates), so it
// nudges too.
export const deleteTask = (id: string) => {
  const [path, opts] = forTask(id, `/tasks/${id}`)

  return nudged(call(path, { ...opts, method: 'DELETE' }), taskGateway(id))
}

type BulkResult = { results: Array<{ id: string; ok: boolean; error?: string }> }

/** One patch, many ids — independent per-id application; returns per-id
 *  outcomes so the UI can toast partial failures. [daveey] Split per owning
 *  gateway (and board); a whole gateway failing reports each of its ids. */
export async function bulkTasks(ids: string[], patch: Record<string, unknown>): Promise<BulkResult> {
  const groups = new Map<string, string[]>()

  for (const id of ids) {
    const owner = taskOwners.get(id)
    const key = owner && owner.gateway !== routedScope() ? `${owner.gateway}\0${owner.board}` : ''
    groups.set(key, [...(groups.get(key) ?? []), id])
  }

  const outcomes = await Promise.all(
    [...groups.entries()].map(async ([key, group]) => {
      const [gateway, board] = key ? key.split('\0') : [routedScope(), '']
      const path = key ? withSlug('/tasks/bulk', board) : withBoard('/tasks/bulk')

      try {
        return await nudged(
          call<BulkResult>(path, { ...(key ? via(gateway) : {}), method: 'POST', body: { ids: group, ...patch } }),
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

export const addComment = (id: string, body: string) => {
  const [path, opts] = forTask(id, `/tasks/${id}/comments`)

  return call(path, { ...opts, method: 'POST', body: { author: 'desktop', body } })
}

export const reassignTask = (id: string, profile: string) => {
  const [path, opts] = forTask(id, `/tasks/${id}/reassign`)

  return nudged(call(path, { ...opts, method: 'POST', body: { profile, reclaim_first: true } }), taskGateway(id))
}

export const reclaimTask = (id: string) => {
  const [path, opts] = forTask(id, `/tasks/${id}/reclaim`)

  return nudged(call(path, { ...opts, method: 'POST', body: {} }), taskGateway(id))
}

export const uploadAttachment = (
  id: string,
  upload: { filename: string; contentType?: string; bytes: ArrayBuffer }
) => {
  const [path, opts] = forTask(id, `/tasks/${id}/attachments`)

  return call(path, { ...opts, method: 'POST', upload })
}

export const createBoard = (slug: string, name: string, projectId?: string) =>
  call<{ board: { slug: string } }>('/boards', {
    method: 'POST',
    body: { slug, name, ...(projectId ? { project_id: projectId } : {}) }
  })

/** Rough auxiliary-model estimate for a task (tokens + complexity). Makes a
 *  model call — gate behind an explicit user action + disclaimer. */
export const estimateTask = (id: string) => {
  const [path, opts] = forTask(id, `/tasks/${id}/estimate`)

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
