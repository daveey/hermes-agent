/**
 * Typed `desktop.ui_latency` producer: click → the result the user can see.
 *
 * Plugins read it with `host.onEvent('desktop.ui_latency', …)`. Spans:
 *   ui.session_switch  click → the matching transcript is committed AND painted
 *   ui.new_session     click → the new composer is visible and editable
 *   ui.card_read       click → the matching card detail is committed AND painted
 *   ui.card_create     click → the card was created (no paint wait: the API answer is the result)
 *
 * Rules (each is covered by `ui-latency.test.ts`):
 *   - "After paint" is two requestAnimationFrames past the commit that showed the result. A store
 *     update or a request completing is NOT a paint and is never substituted for one.
 *   - A span ends only through `finishUiLatencyAfterPaint` / `completeUiLatency`. A failure, a
 *     superseding click, a hidden window or the time bound CANCELS it: nothing is emitted.
 *   - One pending span per name; the session switch and new-session spans share one navigation
 *     channel, so a later click on either supersedes the earlier one. At most 3 spans are pending.
 *   - Payload is identifiers only. `start_ms` is the click's Unix epoch (Date.now); `duration_ms`
 *     comes from performance.now(), so a wall-clock step cannot make it negative.
 */

import { atom } from 'nanostores'

import { emitGatewayEvent } from '@/contrib/events'

export type UiLatencyName = 'ui.card_create' | 'ui.card_read' | 'ui.new_session' | 'ui.session_switch'

export interface UiLatencyIds {
  board?: string
  card_id?: string
  gateway?: string
  profile?: string
  session_id?: string
}

/** Facts a finisher may require before the span can end (the visible result must be THIS click's). */
export interface UiLatencyExpect {
  /** Runtime id the new composer must be bound to; `null` = an unbound fresh draft. */
  runtimeId?: null | string
}

interface Span {
  expect: UiLatencyExpect
  finishing: boolean
  ids: UiLatencyIds
  name: UiLatencyName
  startEpoch: number
  startMono: number
  timer: ReturnType<typeof setTimeout>
}

/** A result that never shows (stuck load, dismissed dialog) is dropped, not reported late. */
export const UI_LATENCY_MAX_MS = 15_000

/** `expect.runtimeId` until the session a click is creating exists: no composer can match it before `bind`. */
export const UI_LATENCY_UNBOUND = '\u0000unbound'

const CHANNEL: Record<UiLatencyName, string> = {
  'ui.card_create': 'card_create',
  'ui.card_read': 'card_read',
  'ui.new_session': 'nav',
  'ui.session_switch': 'nav'
}

const pending = new Map<string, Span>()
let visibilityBound = false

/** Bumped whenever a span starts or learns new facts, so a mounted finisher re-checks what is already on screen. */
export const $uiLatencyEpoch = atom(0)

const monoNow = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())

function dropChannel(channel: string): void {
  const span = pending.get(channel)

  if (span) {
    clearTimeout(span.timer)
    pending.delete(channel)
  }
}

function bindVisibility(): void {
  if (visibilityBound || typeof document === 'undefined') {
    return
  }

  visibilityBound = true
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      cancelAllUiLatency()
    }
  })
}

const clean = (ids: UiLatencyIds): UiLatencyIds =>
  Object.fromEntries(Object.entries(ids).filter(([, value]) => typeof value === 'string' && value)) as UiLatencyIds

/** The click. Supersedes (cancels) any earlier span on the same channel. */
export function startUiLatency(name: UiLatencyName, ids: UiLatencyIds = {}, expect: UiLatencyExpect = {}): void {
  bindVisibility()

  const channel = CHANNEL[name]
  dropChannel(channel)

  const span: Span = {
    expect,
    finishing: false,
    ids: clean(ids),
    name,
    startEpoch: Date.now(),
    startMono: monoNow(),
    timer: setTimeout(() => {
      if (pending.get(channel) === span) {
        pending.delete(channel)
      }
    }, UI_LATENCY_MAX_MS)
  }

  pending.set(channel, span)
  $uiLatencyEpoch.set($uiLatencyEpoch.get() + 1)
}

/** Add facts learned after the click (the created session's ids, the runtime the composer must show). */
export function bindUiLatency(name: UiLatencyName, ids: UiLatencyIds = {}, expect: UiLatencyExpect = {}): void {
  const span = pending.get(CHANNEL[name])

  if (span?.name === name && !span.finishing) {
    span.ids = { ...span.ids, ...clean(ids) }
    span.expect = { ...span.expect, ...expect }
    $uiLatencyEpoch.set($uiLatencyEpoch.get() + 1)
  }
}

/** The current span of `name`, if any (finishers read what to match against). */
export function peekUiLatency(name: UiLatencyName): { expect: UiLatencyExpect; ids: UiLatencyIds } | null {
  const span = pending.get(CHANNEL[name])

  return span?.name === name && !span.finishing ? { expect: span.expect, ids: span.ids } : null
}

/** Failure, supersession by something else, or the result never appeared: end without emitting. */
export function cancelUiLatency(name: UiLatencyName): void {
  const channel = CHANNEL[name]

  if (pending.get(channel)?.name === name) {
    dropChannel(channel)
  }
}

export function cancelAllUiLatency(): void {
  for (const channel of [...pending.keys()]) {
    dropChannel(channel)
  }
}

function emit(span: Span): void {
  emitGatewayEvent({
    payload: {
      ...span.ids,
      duration_ms: Math.max(0, Math.round(monoNow() - span.startMono)),
      name: span.name,
      start_ms: span.startEpoch
    },
    type: 'desktop.ui_latency'
  })
}

/**
 * The result was committed: end the span two animation frames later (the first frame lets the commit
 * paint, the second runs after that paint). `match` names the ids the committed result must carry —
 * a commit for another session/card leaves the span pending. `stillValid` is re-read after the
 * frames; false (the result is gone again, the pane hid) cancels instead of emitting.
 */
export function finishUiLatencyAfterPaint(
  name: UiLatencyName,
  match: Pick<UiLatencyIds, 'card_id' | 'session_id'> = {},
  stillValid: () => boolean = () => true
): void {
  const channel = CHANNEL[name]
  const span = pending.get(channel)

  if (!span || span.name !== name || span.finishing) {
    return
  }

  if (
    (match.session_id && span.ids.session_id !== match.session_id) ||
    (match.card_id && span.ids.card_id !== match.card_id)
  ) {
    return
  }

  if (
    typeof requestAnimationFrame !== 'function' ||
    (typeof document !== 'undefined' && document.visibilityState === 'hidden')
  ) {
    dropChannel(channel)

    return
  }

  span.finishing = true

  requestAnimationFrame(() =>
    requestAnimationFrame(() => {
      if (pending.get(channel) !== span) {
        return
      }

      dropChannel(channel)

      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
        return
      }

      let valid = false

      try {
        valid = stillValid()
      } catch {
        valid = false
      }

      if (valid) {
        emit(span)
      }
    })
  )
}

/** A result with no paint to wait for (the create call succeeded). */
export function completeUiLatency(name: UiLatencyName, ids: UiLatencyIds = {}): void {
  const channel = CHANNEL[name]
  const span = pending.get(channel)

  if (!span || span.name !== name || span.finishing) {
    return
  }

  dropChannel(channel)
  span.ids = { ...span.ids, ...clean(ids) }
  emit(span)
}

/** Test seam. */
export function resetUiLatencyForTests(): void {
  cancelAllUiLatency()
}
