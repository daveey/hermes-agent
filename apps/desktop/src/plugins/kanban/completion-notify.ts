/**
 * Native kanban terminal-event notification (completion, blocker, failure).
 *
 * No maintained exact-fit OSS exists and the SDK
 * has no kanban event door, so this module rides the kanban plugin's EXISTING
 * /events socket (api.ts onEventsFrame). No new WebSocket, no new process,
 * no DB, no auth, no persistence — cursor is an in-memory per-board high-water
 * mark. Notifies on the same terminal kinds the gateway watcher pings
 * (gateway/kanban_watchers.py): 'completed' (kanban_db.complete_task —
 * payload: summary + artifacts), 'blocked' (payload: reason), 'gave_up'
 * (payload: error), 'crashed', 'timed_out', and 'block_loop_detected'
 * (payload: reason — the routed-to-triage orchestration handoff).
 *
 * Two delivery doors, complementary by design:
 *  - `host.notify` — the in-app toast, covers the foreground case;
 *  - `ctx.os.notify` (when bound) — the native OS notification, which the
 *    desktop shell fires only while the user is AWAY from Hermes. This is the
 *    door that covers "walked away and the worker hit a blocker".
 *
 * Cursor contract: first observation of a board baselines
 * seen[board] = GET /board latest_event_id (MAX task_events.id for that
 * board). Events id <= seen are historical/replay — never notified, no
 * cursor change. id > seen advances cursor for EVERY kind; only terminal
 * kinds emit. Reconnect replays from 0; cursor filters. Board switch never
 * mixes cursors; returning reuses prior cursor (never reset to current MAX).
 * Fail-closed: while a board's baseline is unknown, no event can be
 * classified so none is notified. Empty slug ('') suppressed.
 */

import { host, type PluginOs, type PluginRestOptions, type PluginTranslate } from '@hermes/plugin-sdk'

import { en } from './i18n'
import { $openTaskRequest } from './open-task'

type Rest = <T>(path: string, opts?: PluginRestOptions) => Promise<T>

export interface CompletionEvent {
  id?: unknown
  task_id?: string
  kind?: string
  payload?: Record<string, unknown> | null
}

type ToastKind = 'error' | 'success' | 'warning'

interface NotifySpec {
  titleKey: string
  toast: ToastKind
  /** Codicon + tint for the toast's leading icon, so the task's state reads at a
   *  glance: green check = done, yellow ? = blocked / waiting on someone,
   *  red X = routed to triage or failed. */
  icon: string
  accentColor: string
  /** Same glyph for the native OS notification title (no custom icon there). */
  glyph: string
}

const DONE = { accentColor: 'var(--ui-green)', glyph: '\u2705', icon: 'pass-filled' } as const
const WAITING = { accentColor: 'var(--ui-yellow)', glyph: '\u2753', icon: 'question' } as const
const TRIAGE = { accentColor: 'var(--ui-red)', glyph: '\u274C', icon: 'error' } as const

/** Terminal kinds → toast severity + i18n title key + status icon. Mirrors the
 *  gateway watcher's ping set (gateway/kanban_watchers.py) minus the
 *  intentionally silent kinds (status/archived/unblocked, which only advance
 *  the cursor). `block_loop_detected` is the routed-to-triage handoff. */
const TERMINAL_NOTIFY = new Map<string, NotifySpec>([
  ['blocked', { titleKey: 'notify.blockedTitle', toast: 'warning', ...WAITING }],
  ['block_loop_detected', { titleKey: 'notify.blockLoopTitle', toast: 'warning', ...TRIAGE }],
  ['completed', { titleKey: 'notify.completedTitle', toast: 'success', ...DONE }],
  ['crashed', { titleKey: 'notify.crashedTitle', toast: 'error', ...TRIAGE }],
  ['gave_up', { titleKey: 'notify.gaveUpTitle', toast: 'error', ...TRIAGE }],
  ['timed_out', { titleKey: 'notify.timedOutTitle', toast: 'warning', ...WAITING }]
])

const seenEventIdByBoard = new Map<string, number>()
const baselinePending = new Set<string>()

let rest: Rest | null = null
let translate: PluginTranslate | null = null
let osDoor: PluginOs | null = null
let silentKinds: ReadonlySet<string> = new Set()
let toastlessKinds: ReadonlySet<string> = new Set()

/** [daveey] Per-kind delivery overrides. `silent` kinds fire nothing (no toast,
 *  no OS notification); `toastless` kinds skip the in-app toast but keep the
 *  native OS notification the shell shows only while the user is away. Both
 *  still advance the cursor. Unset = every terminal kind toasts (upstream). */
export interface CompletionNotifyQuiet {
  silent?: readonly string[]
  toastless?: readonly string[]
}

/** Resolve a dot-path against the plugin's own English bundle — the same
 *  last-rung fallback the plugin i18n registry applies, usable before (or
 *  without) a bound translator. */
function fallbackT(key: string, ...args: unknown[]): string {
  let node: unknown = en

  for (const part of key.split('.')) {
    node = (node as Record<string, unknown> | undefined)?.[part]
  }

  if (typeof node === 'function') {
    return (node as (...a: unknown[]) => string)(...args)
  }

  return typeof node === 'string' ? node : key
}

function t(key: string, ...args: unknown[]): string {
  const translated = translate?.(key, ...args)

  // The registry returns the raw key when the bundle isn't registered yet.
  return translated && translated !== key ? translated : fallbackT(key, ...args)
}

export function bindCompletionNotify(
  r: Rest,
  pluginTranslate?: PluginTranslate,
  os?: PluginOs,
  quiet?: CompletionNotifyQuiet
): void {
  rest = r
  translate = pluginTranslate ?? null
  osDoor = os ?? null
  silentKinds = new Set(quiet?.silent ?? [])
  toastlessKinds = new Set(quiet?.toastless ?? [])
}

async function ensureBaseline(slug: string): Promise<void> {
  if (seenEventIdByBoard.has(slug) || baselinePending.has(slug)) {
    return
  }

  baselinePending.add(slug)

  try {
    const board = (await rest!<{ latest_event_id?: unknown }>(`/board?board=${encodeURIComponent(slug)}`)) as {
      latest_event_id?: unknown
    }

    seenEventIdByBoard.set(slug, typeof board.latest_event_id === 'number' ? board.latest_event_id : 0)
  } catch {
    // Fail-closed: unknown baseline → notifications stay suppressed.
  } finally {
    baselinePending.delete(slug)
  }
}

function trimmed(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/** The human handoff carried in the event payload, per kind (mirrors the
 *  payload contract the gateway watcher reads). `gave_up` deliberately has no
 *  payload body: its `error` is raw worker text, which belongs in the toast
 *  `detail` (see rawErrorFor), and the body is the plain-words i18n hint. */
function bodyFor(kind: string, ev: CompletionEvent): string {
  const payload = ev.payload

  if (kind === 'completed') {
    return trimmed(payload?.summary)
  }

  // [daveey] A block's user-facing step comes only from its structured ask (human_request.action). The
  // payload `reason` is the agent's own status note ("Architecture choice settled; …") and is never the body.
  if (kind === 'blocked') {
    return humanAsk(payload)?.action ?? t('notify.pausedBody')
  }

  if (kind === 'block_loop_detected') {
    return trimmed(payload?.reason)
  }

  if (kind === 'gave_up') {
    return t('notify.gaveUpBody')
  }

  return ''
}

const ASK_MAX = 300

/** [daveey] The block's structured ask: `human_request.action` (+ `why_human`), clipped for a notification.
 *  A dict or JSON text; anything without a string action is no ask. */
export function humanAsk(payload: Record<string, unknown> | null | undefined): { action: string; why: string } | null {
  let r: unknown = payload?.human_request

  if (typeof r === 'string') {
    try {
      r = JSON.parse(r)
    } catch {
      return null
    }
  }

  if (!r || typeof r !== 'object' || Array.isArray(r)) {
    return null
  }

  const clip = (v: unknown) => {
    const s = trimmed(v).replace(/\s+/g, ' ')

    return s.length > ASK_MAX ? `${s.slice(0, ASK_MAX - 1).trimEnd()}\u2026` : s
  }

  const action = clip((r as Record<string, unknown>).action)

  return action ? { action, why: clip((r as Record<string, unknown>).why_human) } : null
}

/** Raw machine text that must never be the toast body — surfaced muted in `detail`. */
function rawErrorFor(kind: string, ev: CompletionEvent): string {
  return kind === 'gave_up' ? trimmed(ev.payload?.error) : ''
}

function notifyOne(
  slug: string,
  kind: string,
  spec: NotifySpec,
  ev: CompletionEvent
): void {
  const taskId = (ev.task_id ?? '').trim()
  const body = bodyFor(kind, ev)

  const artifacts =
    kind === 'completed' && Array.isArray(ev.payload?.artifacts)
      ? (ev.payload!.artifacts as unknown[])
          .filter((a): a is string => typeof a === 'string' && a.trim().length > 0)
          .map(a => a.trim())
      : []

  const artifactText =
    artifacts.length === 1
      ? artifacts[0].split(/[\\/]/).pop() || artifacts[0]
      : artifacts.length > 1
        ? t('notify.artifacts', artifacts.length)
        : ''

  const ask = kind === 'blocked' ? humanAsk(ev.payload) : null
  const why = ask?.why ? t('notify.whyYou', ask.why) : ''
  const detail = [why, taskId, artifactText, rawErrorFor(kind, ev)].filter(Boolean).join(' · ')
  const title = kind === 'blocked' && !ask ? t('notify.pausedTitle') : t(spec.titleKey)
  const message = body || taskId || title

  if (!toastlessKinds.has(kind)) {
    host.notify({
      kind: spec.toast,
      icon: spec.icon,
      accentColor: spec.accentColor,
      title,
      message,
      ...(detail ? { detail } : {}),
      action: {
        label: t('notify.openKanban'),
        onClick: () => {
          // Land on the event's board with the task's drawer open, not just
          // whatever board the page last showed.
          if (taskId) {
            $openTaskRequest.set({ board: slug, taskId })
          }

          host.navigate('/kanban')
        }
      }
    })
  }

  // Native OS notification — the desktop shell fires it only while the user
  // is away from Hermes (the toast above covers the foreground case). Isolated:
  // a missing/broken shell must not mark the toast as unfired.
  try {
    osDoor?.notify({ title: `${spec.glyph} ${title}`, body: [message, detail].filter(Boolean).join('\n') })
  } catch {
    /* swallowed */
  }
}

/** Consume one /events frame for a board. Returns true when a terminal-event
 *  notification was fired. Never throws: notification failure cannot
 *  interfere with api.ts cache invalidation. */
export async function onKanbanEventsFrame(slug: string, events?: CompletionEvent[]): Promise<boolean> {
  if (!events?.length || slug === '' || !rest) {
    return false
  }

  await ensureBaseline(slug)
  const seen = seenEventIdByBoard.get(slug)

  if (seen === undefined) {
    return false
  } // fail-closed

  let fired = false
  let cursor = seen

  for (const ev of events) {
    if (typeof ev.id !== 'number' || ev.id <= cursor) {
      continue
    }

    cursor = ev.id
    seenEventIdByBoard.set(slug, cursor)
    const spec = TERMINAL_NOTIFY.get(ev.kind ?? '')

    if (spec && !silentKinds.has(ev.kind!)) {
      try {
        notifyOne(slug, ev.kind!, spec, ev)
        fired = true
      } catch {
        /* swallowed */
      }
    }
  }

  return fired
}
