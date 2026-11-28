import { atom } from 'nanostores'

import { translateNow } from '@/i18n'
import { type HermesOpenTarget, resolveHermesOpenPath } from '@/lib/hermes-open-target'
import { persistString, storedString } from '@/lib/storage'

import { recordFeatureToggle } from './desktop-metrics'
import { $gateway, primaryGatewayConnectionId } from './gateway'
import { $hapticsMuted } from './haptics'
import { hasFreshKanbanObservation } from './kanban-observation'
import { withinNativeNotifyBaseline } from './notify-baseline'
import {
  answerApproval,
  clearApprovalRequest,
  replayPendingApproval,
  sessionApprovalRequest,
  sessionApprovalRequests
} from './prompts'
import { isSessionGone, isSessionGoneForBackgroundPolling, markSessionGone } from './runtime-gone'
import {
  $activeSessionId,
  $cronSessions,
  $messagingSessions,
  $sessions,
  ownerLookupSessionRows,
  sessionMatchesStoredId
} from './session'
import {
  $sessionStates,
  holdsTurnClaim,
  knownOwnerForSession,
  runtimeSessionOwner,
  storedSessionIdForRuntimeId
} from './session-states'

export type { HermesOpenTarget }

// Native OS notifications (Electron `Notification`), separate from the in-app
// toast feed in `notifications.ts`. Each kind toggles independently.
export type NativeNotificationKind =
  'approval' | 'backgroundDone' | 'credits' | 'input' | 'plugin' | 'turnDone' | 'turnError'

export const NATIVE_NOTIFICATION_KINDS: readonly NativeNotificationKind[] = [
  'approval',
  'input',
  'turnDone',
  'turnError',
  'backgroundDone',
  'credits',
  'plugin'
]

// Blocking prompts — surface even while focused if they're for another session.
const ATTENTION_KINDS = new Set<NativeNotificationKind>(['approval', 'input'])

export interface NativeNotificationPrefs {
  enabled: boolean
  kinds: Record<NativeNotificationKind, boolean>
}

const STORAGE_KEY = 'hermes:native-notifications'

const DEFAULT_PREFS: NativeNotificationPrefs = {
  enabled: true,
  kinds: {
    approval: true,
    backgroundDone: true,
    credits: true,
    input: true,
    plugin: true,
    turnDone: true,
    turnError: true
  }
}

function readPrefs(): NativeNotificationPrefs {
  const raw = storedString(STORAGE_KEY)

  if (!raw) {
    return DEFAULT_PREFS
  }

  try {
    const parsed = JSON.parse(raw) as Partial<NativeNotificationPrefs>
    const kinds = { ...DEFAULT_PREFS.kinds }

    for (const kind of NATIVE_NOTIFICATION_KINDS) {
      const value = parsed.kinds?.[kind]

      if (typeof value === 'boolean') {
        kinds[kind] = value
      }
    }

    return {
      enabled: typeof parsed.enabled === 'boolean' ? parsed.enabled : DEFAULT_PREFS.enabled,
      kinds
    }
  } catch {
    return DEFAULT_PREFS
  }
}

export const $nativeNotifyPrefs = atom<NativeNotificationPrefs>(readPrefs())

function writePrefs(next: NativeNotificationPrefs) {
  $nativeNotifyPrefs.set(next)
  persistString(STORAGE_KEY, JSON.stringify(next))
}

export function setNativeNotifyEnabled(enabled: boolean) {
  recordFeatureToggle('native_notifications', $nativeNotifyPrefs.get().enabled, enabled)
  writePrefs({ ...$nativeNotifyPrefs.get(), enabled })
}

export function setNativeNotifyKind(kind: NativeNotificationKind, on: boolean) {
  const prev = $nativeNotifyPrefs.get()
  recordFeatureToggle('notification_kind', prev.kinds[kind], on)
  writePrefs({ ...prev, kinds: { ...prev.kinds, [kind]: on } })
}

// De-dupe replayed events for the same kind+session. Self-evicting: entries
// older than the window are pruned on every dispatch, so the map can't grow.
const THROTTLE_MS = 1000
const lastFiredAt = new Map<string, number>()

function throttled(key: string, now: number): boolean {
  for (const [k, at] of lastFiredAt) {
    if (now - at >= THROTTLE_MS) {
      lastFiredAt.delete(k)
    }
  }

  if (lastFiredAt.has(key)) {
    return true
  }

  lastFiredAt.set(key, now)

  return false
}

// "Backgrounded" = the user isn't on Hermes. `document.hidden` only flips when
// minimized/occluded; an alt-tabbed window is visible-but-unfocused, so we also
// check `document.hasFocus()`.
function isBackgrounded(): boolean {
  if (typeof document === 'undefined') {
    return false
  }

  if (document.hidden) {
    return true
  }

  return typeof document.hasFocus === 'function' && !document.hasFocus()
}

function shouldFire(kind: NativeNotificationKind, sessionId?: null | string, global = false): boolean {
  // Global notifications aren't tied to a chat session (e.g. pet generation,
  // which runs from the command center with no active conversation). They fire
  // whenever the user is away, with no session-match requirement — otherwise a
  // background run started without an open session would be silently dropped.
  if (global) {
    return isBackgrounded()
  }

  // Attention kinds break through for an off-screen session even while focused.
  if (ATTENTION_KINDS.has(kind)) {
    return isBackgrounded() || (Boolean(sessionId) && sessionId !== $activeSessionId.get())
  }

  // Completion kinds: only the active session, only while away — so a busy
  // gateway (messaging, kanban, cron) can't spam a toast per background session.
  return isBackgrounded() && Boolean(sessionId) && sessionId === $activeSessionId.get()
}

/** Last-resort label for a session the renderer has no row for (yet): the id
 *  tail still tells three parked approvals apart. */
const shortSessionId = (id: string) => `#${id.slice(-6)}`

/** Blocking prompts name their session in the title ("Approval needed — Fix the
 *  flaky test") so parallel parked approvals stay tellable apart; the caller's
 *  `title` stays the bare fallback for a prompt with no session. */
const NAMED_TITLE_KEYS: Partial<Record<NativeNotificationKind, string>> = {
  approval: 'notifications.native.approvalTitleNamed',
  input: 'notifications.native.inputTitleNamed'
}

/** Sidebar naming order (title → preview → short id) via the locale's
 *  named-title template; the runtime id the caller already passes is the whole
 *  hint needed. */
function withSessionLabel(namedKey: string, runtimeSessionId: string): string {
  const storedId = storedSessionIdForRuntimeId(runtimeSessionId) ?? runtimeSessionId
  const row = ownerLookupSessionRows().find(session => sessionMatchesStoredId(session, storedId))
  const name = row?.title?.trim() || row?.preview?.trim() || shortSessionId(storedId)

  return translateNow(namedKey, name.length > 80 ? `${name.slice(0, 80).trimEnd()}…` : name)
}

export interface NativeNotificationAction {
  id: string
  text: string
  /** Serializable activate target echoed back on button press (plugin path). */
  activate?: string
}

export interface NativeNotificationInput {
  kind: NativeNotificationKind
  title: string
  body?: string
  sessionId?: null | string
  /** Wait for an origin projection observed after this turn ended (epoch seconds). */
  projectionAfter?: number
  /** Existing session-list refresh, retained for background release checks. */
  refreshProjection?: (shouldPublish?: () => boolean) => Promise<void>
  /**
   * Not tied to a chat session (e.g. pet generation). Fires whenever the user
   * is away, bypassing the session-match gate that completion kinds normally
   * require.
   */
  global?: boolean
  silent?: boolean
  actions?: NativeNotificationAction[]
  /**
   * Extra throttle/dedupe discriminator for session-less notifications (e.g.
   * the plugin id), so unrelated emitters of the same kind don't collapse
   * into one another. Never drives click-to-focus like `sessionId` does.
   */
  tag?: string
  /** Absolute file path for the OS notification icon (Electron). */
  icon?: string
  /**
   * Resolved hash-router path to open on body click when there is no
   * `sessionId` (plugins). Same vocabulary as `hermes://index-network/intent/1`.
   */
  activate?: string
  /** Renderer-side handle so click/action can invoke registered callbacks. */
  notifyId?: string
}

type DeferredCompletion = { input: NativeNotificationInput; storedId: string; owner: string }
const deferredCompletions = new Map<string, DeferredCompletion>()
let stopCompletionWatch: (() => void) | undefined

function projectionOwner(row: { connection_id?: string; profile?: string }): string {
  return JSON.stringify([row.connection_id ?? primaryGatewayConnectionId(), row.profile ?? 'default'])
}

function deferCompletion(input: NativeNotificationInput, storedId: string, owner: string): void {
  const runtime = input.sessionId

  if (!runtime || !$sessionStates.get()[runtime]) {
    return
  }

  // Bounded, in-memory pending notices; never restore an old turn on app restart.
  if (deferredCompletions.size >= 128 && !deferredCompletions.has(runtime)) {
    return
  }

  deferredCompletions.set(runtime, { input: { ...input }, storedId, owner })

  if (stopCompletionWatch) {
    return
  }

  const retry = () => {
    for (const [id, pending] of [...deferredCompletions]) {
      const state = $sessionStates.get()[id]

      if (!state || holdsTurnClaim(state)) {
        deferredCompletions.delete(id)

        continue
      }

      const currentId = storedSessionIdForRuntimeId(id)
      const owner = runtimeSessionOwner(id)

      const runtimePin =
        typeof owner === 'string'
          ? projectionOwner({ profile: owner })
          : owner
            ? projectionOwner({ connection_id: owner.connectionId, profile: owner.profile })
            : undefined

      if (runtimePin !== undefined && runtimePin !== pending.owner) {
        deferredCompletions.delete(id)

        continue
      }

      const candidates = ownerLookupSessionRows().filter(row => sessionMatchesStoredId(row, pending.storedId))
      const rows = candidates.filter(row => projectionOwner(row) === pending.owner)

      if (currentId !== pending.storedId) {
        // Compression may rotate the durable id without starting a new turn.
        // Only one same-owner row proving BOTH aliases can carry the notice.
        if (!currentId || rows.length !== 1 || !sessionMatchesStoredId(rows[0], currentId)) {
          deferredCompletions.delete(id)

          continue
        }

        pending.storedId = currentId
      }

      if (rows.length !== 1) {
        if (candidates.length === 1 && runtimePin === undefined) {
          deferredCompletions.delete(id)
        }

        continue
      }

      // Delete before dispatch: it may requeue only if the projection is still
      // unfinished. Preference/focus changes intentionally discard the notice.
      deferredCompletions.delete(id)
      dispatchNativeNotification(pending.input)
    }

    if (!deferredCompletions.size) {
      stopCompletionWatch?.()
      stopCompletionWatch = undefined
    }
  }

  const stops = [
    $sessions.listen(retry),
    $cronSessions.listen(retry),
    $messagingSessions.listen(retry),
    $sessionStates.listen(retry)
  ]

  let stopped = false
  let timer: ReturnType<typeof setTimeout>

  const refresh = async () => {
    retry()

    if (stopped) {
      return
    }

    const callbacks = new Set([...deferredCompletions.values()].map(p => p.input.refreshProjection))
    // refreshSessions already accepts a publication guard. Time out this read
    // without allowing a late page to overwrite a newer retry or owner state.
    let current = true
    let deadline: ReturnType<typeof setTimeout> | undefined

    try {
      await Promise.race([
        Promise.allSettled(
          [...callbacks]
            .filter(Boolean)
            .map(callback => Promise.resolve().then(() => callback?.(() => current && !stopped)))
        ),
        new Promise<void>(resolve => {
          deadline = setTimeout(resolve, 15000)
        })
      ])
    } finally {
      current = false
      clearTimeout(deadline)
    }

    if (!stopped) {
      retry()

      if (!stopped) {
        timer = setTimeout(() => {
          void refresh()
        }, 15000)
      }
    }
  }

  timer = setTimeout(() => {
    void refresh()
  }, 15000)

  stopCompletionWatch = () => {
    stopped = true
    clearTimeout(timer)
    stops.forEach(stop => stop())
  }
}

function completionOwnerPin(sessionId: string): string | undefined {
  const owner = runtimeSessionOwner(sessionId) ?? knownOwnerForSession(sessionId)

  return typeof owner === 'string'
    ? projectionOwner({ profile: owner })
    : owner
      ? projectionOwner({ connection_id: owner.connectionId, profile: owner.profile })
      : undefined
}

/** A terminal turn is not completion of its dispatched work: consult the ledger projection. */
function deferUnfinishedCompletion(input: NativeNotificationInput): boolean {
  if (input.kind !== 'turnDone' || !input.sessionId) {
    return false
  }

  const storedId = storedSessionIdForRuntimeId(input.sessionId) ?? input.sessionId
  const ownerPin = completionOwnerPin(input.sessionId)

  const rows = ownerLookupSessionRows().filter(
    row => sessionMatchesStoredId(row, storedId) && (ownerPin === undefined || projectionOwner(row) === ownerPin)
  )

  const row = rows.length === 1 ? rows[0] : undefined
  const known = hasFreshKanbanObservation(row, input.projectionAfter)

  const unfinished =
    !row ||
    !known ||
    row.kanban_unknown === true ||
    (row.kanban_active ?? 0) > 0 ||
    (row.kanban_blocked ?? 0) > 0 ||
    (row.kanban_remote_stale ?? 0) > 0

  if (unfinished) {
    if (input.projectionAfter !== undefined) {
      const pin = ownerPin ?? (row ? projectionOwner(row) : undefined)

      if (pin !== undefined) {
        deferCompletion(input, storedId, pin)
      }
    }

    return true
  }

  deferredCompletions.delete(input.sessionId)

  return false
}

/** Returns true when the notification passed every guard and was handed to the
 *  OS bridge — callers registering per-notification state (plugin handlers)
 *  must only do so on true, or suppressed/throttled notifications leak it. */
export function dispatchNativeNotification(input: NativeNotificationInput): boolean {
  const prefs = $nativeNotifyPrefs.get()

  if (!prefs.enabled || !prefs.kinds[input.kind]) {
    return false
  }

  if (withinNativeNotifyBaseline()) {
    return false
  }

  if (!shouldFire(input.kind, input.sessionId, input.global)) {
    return false
  }

  if (deferUnfinishedCompletion(input)) {
    return false
  }

  if (throttled(`${input.kind}:${input.sessionId ?? input.tag ?? (input.global ? 'global' : '')}`, Date.now())) {
    return false
  }

  const namedKey = input.sessionId ? NAMED_TITLE_KEYS[input.kind] : undefined
  const title = namedKey && input.sessionId ? withSessionLabel(namedKey, input.sessionId) : input.title

  void window.hermesDesktop?.notify({
    actions: input.actions,
    activate: input.activate,
    body: input.body,
    focusSessionId: input.sessionId ? (storedSessionIdForRuntimeId(input.sessionId) ?? undefined) : undefined,
    icon: input.icon,
    kind: input.kind,
    notifyId: input.notifyId,
    sessionId: input.sessionId ?? undefined,
    // The app-sounds mute also silences OS notification sounds.
    silent: $hapticsMuted.get() || input.silent,
    tag: input.tag,
    title
  })

  return true
}

// -- the plugin door (`ctx.os.notify`) ----------------------------------------

export interface PluginNotificationAction {
  id: string
  label: string
  /** Navigate here on button press (path or `hermes://index-network/intent/1`). */
  activate?: HermesOpenTarget
  /** Renderer callback — only `id` crosses IPC; this stays in-process. */
  onAction?: () => void
}

export interface PluginNativeNotificationInput {
  title: string
  body?: string
  silent?: boolean
  /** Absolute filesystem path for the notification icon. */
  icon?: string
  /**
   * Where body-click should land. Accepts a plugin deep link
   * (`hermes://index-network/intent/1`), a hash path (`/index-network/intent/1`),
   * or `{ path, params }` — all resolve through the same helper as OS deep links.
   */
  activate?: HermesOpenTarget
  /** Extra work on body click (runs in addition to `activate` navigation). */
  onActivate?: () => void
  actions?: PluginNotificationAction[]
}

interface PendingPluginNotify {
  onActivate?: () => void
  actions: Map<string, () => void>
}

const pendingPluginNotify = new Map<string, PendingPluginNotify>()

function mintNotifyId(pluginId: string): string {
  return `${pluginId}:${Date.now().toString(36)}:${Math.random().toString(36).slice(2, 8)}`
}

/** Invoke body-click callback if one was registered for this notify id. */
export function invokePluginNotifyActivate(notifyId: string | undefined): void {
  if (!notifyId) {
    return
  }

  const pending = pendingPluginNotify.get(notifyId)
  pending?.onActivate?.()
}

/** Invoke an action-button callback. Returns true when a handler ran. */
export function invokePluginNotifyAction(notifyId: string | undefined, actionId: string | undefined): boolean {
  if (!notifyId || !actionId) {
    return false
  }

  const handler = pendingPluginNotify.get(notifyId)?.actions.get(actionId)

  if (!handler) {
    return false
  }

  handler()

  return true
}

/** Drop pending handlers (tests / after a click consumed the toast). */
export function clearPluginNotifyHandlers(notifyId?: string): void {
  if (notifyId) {
    pendingPluginNotify.delete(notifyId)

    return
  }

  pendingPluginNotify.clear()
}

/** Native OS notification on behalf of a plugin. One "Plugin notifications"
 *  preference gates all plugins; the plugin id keys throttling/dedupe so two
 *  plugins can't collapse each other's notifications. Fires only while the
 *  user is away from Hermes — the in-app toast (`host.notify`) covers the
 *  foreground case. */
export function dispatchPluginNativeNotification(pluginId: string, input: PluginNativeNotificationInput): void {
  const activate = resolveHermesOpenPath(input.activate) ?? undefined
  const notifyId = input.onActivate || input.actions?.some(a => a.onAction) ? mintNotifyId(pluginId) : undefined

  const actions: NativeNotificationAction[] | undefined = input.actions?.map(action => ({
    activate: resolveHermesOpenPath(action.activate) ?? undefined,
    id: action.id,
    text: action.label
  }))

  const fired = dispatchNativeNotification({
    actions,
    activate,
    body: input.body,
    global: true,
    icon: input.icon,
    kind: 'plugin',
    notifyId,
    // The app-sounds mute also silences OS notification sounds.
    silent: $hapticsMuted.get() || input.silent,
    tag: pluginId,
    title: input.title
  })

  // Register renderer callbacks only for notifications that actually reached
  // the OS — a throttled/suppressed one can never be clicked, so registering
  // first would leak the closures for the window's lifetime.
  if (fired && notifyId) {
    const handlers = new Map<string, () => void>()

    for (const action of input.actions ?? []) {
      if (action.onAction) {
        handlers.set(action.id, action.onAction)
      }
    }

    pendingPluginNotify.set(notifyId, { actions: handlers, onActivate: input.onActivate })
  }
}

// Resolve a pending approval from a notification button, mirroring the in-app
// Run/Reject bar. Keyed by session id — a background approval has no local guard.
export async function respondToApprovalAction(sessionId: null | string, actionId: string): Promise<void> {
  const [action, ...idParts] = actionId.split(':')
  const requestId = idParts.length ? idParts.join(':') : sessionApprovalRequest(sessionId).get()?.requestId
  const choice = action === 'approve' ? 'once' : action === 'reject' ? 'deny' : null

  if (!choice) {
    return
  }

  if (sessionId && isSessionGone(sessionId)) {
    return
  }

  const gateway = $gateway.get()

  if (!gateway) {
    return
  }

  try {
    const parked = sessionApprovalRequests(sessionId)
      .get()
      .find(request => request.requestId === requestId)

    await answerApproval(gateway, parked ?? { sessionId, requestId }, choice)

    if (requestId || sessionApprovalRequest(sessionId).get()?.requestId === undefined) {
      clearApprovalRequest(sessionId, requestId)
    }

    void replayPendingApproval(gateway, sessionId).catch(() => undefined)
  } catch (error) {
    if (sessionId && isSessionGoneForBackgroundPolling(error)) {
      markSessionGone(sessionId)
    }

    // Leave the prompt parked so the user can still resolve it in-app.
  }
}

// Settings "send test" — bypasses gating. Returns whether the OS accepted it so
// the panel can flag a silent permission failure instead of looking dead.
export async function sendTestNativeNotification(title: string, body: string): Promise<boolean> {
  const bridge = window.hermesDesktop

  if (!bridge?.notify) {
    return false
  }

  try {
    return await bridge.notify({ body, kind: 'turnDone', title })
  } catch {
    return false
  }
}
