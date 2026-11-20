import { $messagingSessions, $sessions } from '@/store/session'
import { isSessionParked } from '@/store/session-blocked'
import { $sessionDotStateById, kanbanStripeCounts, sessionStatusStripe, withRowActivity } from '@/store/session-dot-state'
import { sessionHiddenAsAutomated } from '@/store/sidebar-automated'
import type { SessionInfo } from '@/types/hermes'

// The sidebar's session order as the user sees it, for ⌘] / ⌘[ stepping.
// Each rendered SessionsSection (pinned, recents, …) publishes the ids of its
// visible rows plus an anchor element; sections are concatenated in DOM order
// at read time. A section reads its rows straight from the DOM (rows carry
// `data-sidebar-session-id`), because project / profile / messaging groupings
// draw sessions in a different order than the flat row model. Only the
// virtualized flat list renders just its on-screen rows, so there the
// section's row model is the authority.

interface SectionEntry {
  anchor: HTMLElement | null
  ids: readonly string[]
  rows?: readonly SessionInfo[]
  /** Ids of this section's rows that have at least one message (the status
   *  stripe's `hasTurns`), so stepping can match the stripe the row draws. */
  turnIds?: readonly string[]
  virtualized?: boolean
}

const ROW_SELECTOR = '[data-sidebar-session-id]'

function sectionIds(entry: SectionEntry): readonly string[] {
  const root = entry.anchor?.parentElement

  if (entry.virtualized || !root) {
    return entry.ids
  }

  const drawn = [...root.querySelectorAll<HTMLElement>(ROW_SELECTOR)].flatMap(row => {
    const id = row.dataset.sidebarSessionId

    return id ? [id] : []
  })

  // Nothing drawn yet (first paint, or rows still mounting): the model is the
  // best guess. A collapsed section publishes no ids, so this stays empty.
  return drawn.length ? drawn : entry.ids
}

const sections = new Map<symbol, SectionEntry>()

export function publishSidebarSection(key: symbol, entry: SectionEntry): void {
  sections.set(key, entry)
}

export function unpublishSidebarSection(key: symbol): void {
  sections.delete(key)
}

function domOrder(a: SectionEntry, b: SectionEntry): number {
  if (!a.anchor || !b.anchor || a.anchor === b.anchor) {
    return 0
  }

  return a.anchor.compareDocumentPosition(b.anchor) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1
}

/** [daveey] Whether the sidebar keeps this id out as automated / test traffic
 *  (store/sidebar-automated.ts — the predicate the list renderer filters by).
 *  A virtualized section's row model and the recency fallback are not
 *  DOM-filtered, so stepping must apply it itself. */
function hiddenAsAutomated(id: string): boolean {
  const row = $sessions.get().find(s => s.id === id) ?? $messagingSessions.get().find(s => s.id === id)

  return row ? sessionHiddenAsAutomated(row) : false
}

/** Visible sidebar session ids, top to bottom (deduped). Falls back to the
 *  recency list when no section is mounted (sidebar hidden, project view). */
export function sidebarSessionOrder(): string[] {
  const ordered = [...sections.values()].filter(entry => entry.anchor?.isConnected).sort(domOrder)
  const seen = new Set<string>()
  const ids: string[] = []

  for (const entry of ordered) {
    for (const id of sectionIds(entry)) {
      if (!seen.has(id)) {
        seen.add(id)

        if (!hiddenAsAutomated(id)) {
          ids.push(id)
        }
      }
    }
  }

  return ids.length
    ? ids
    : $sessions
        .get()
        .filter(session => !sessionHiddenAsAutomated(session))
        .map(session => session.id)
}

/** Whether a sidebar row shows a coloured status stripe — green (finished,
 *  waiting on you) or red (needs input / stalled). ⌘] / ⌘[ step only to
 *  these, so they land exactly where the sidebar shows a coloured bar. A
 *  session the user parked with ⌘B (grey stripe) is skipped. */
// Only green / red stripes are stops; grey (parked) and the periodic loop
// glyph are skipped like a parked session.
const sessionHasStepStripe = (stripe: null | string | undefined): boolean =>
  stripe === 'done' || stripe === 'stuck' || stripe === 'attention'

export function sessionHasStatusStripe(id: string): boolean {
  if (isSessionParked(id)) {
    return false
  }

  // A drawn row is the authority: step exactly where the sidebar paints a
  // green / red stripe. The projects overview draws lane rows the sections'
  // flat row model doesn't carry, so the model below had no message count
  // for them and skipped green rows there.
  const row = document.querySelector<HTMLElement>(`[data-sidebar-session-id="${CSS.escape(id)}"]`)

  if (row) {
    return sessionHasStepStripe(row.dataset.sidebarStripe)
  }

  // Not drawn (virtualized off-screen row): derive it from the row model.
  let hasTurns = false

  for (const entry of sections.values()) {
    if (entry.turnIds?.includes(id)) {
      hasTurns = true

      break
    }
  }

  const session = [...sections.values()].flatMap(entry => entry.rows ?? []).find(s => s.id === id) ??
    $sessions.get().find(s => s.id === id) ?? $messagingSessions.get().find(s => s.id === id)

  if (!hasTurns) {
    hasTurns = (session?.message_count ?? 0) > 0
  }

  return (
    sessionHasStepStripe(
      sessionStatusStripe(
        withRowActivity($sessionDotStateById.get()[id] ?? 'idle', session ?? {}),
        hasTurns,
        false,
        kanbanStripeCounts(session ?? {}).blocked,
        session?.cron_active === true,
        session?.review_wait,
        session?.kanban_attention ?? 0,
        kanbanStripeCounts(session ?? {}).waiting,
        kanbanStripeCounts(session ?? {}).stalled,
        session?.outcome,
        kanbanStripeCounts(session ?? {}).external,
        kanbanStripeCounts(session ?? {}).externalDown
      )
    )
  )
}

/**
 * The session `direction` steps from `currentId` in `order`, wrapping. With
 * `eligible`, skips sessions it rejects; null when nothing else qualifies.
 * An unknown current id steps from just outside the list (first / last).
 */
export function stepSessionId(
  order: readonly string[],
  currentId: null | string,
  direction: 1 | -1,
  eligible?: (id: string) => boolean
): null | string {
  const n = order.length

  if (n === 0) {
    return null
  }

  const at = currentId ? order.indexOf(currentId) : -1
  const start = at >= 0 ? at : direction === 1 ? -1 : n

  for (let step = 1; step <= n; step += 1) {
    const id = order[(((start + direction * step) % n) + n) % n]

    if (id !== currentId && (!eligible || eligible(id))) {
      return id
    }
  }

  return null
}

/** ⌘] / ⌘[: next session with a status stripe (green / red); null when none
 *  has one. ⇧ (`anySession`) steps to the plain neighbour. `fallbackToAny`
 *  (archive → next) takes the plain neighbour when no row has a stripe.
 *  Sessions blocked with ⌘B are skipped by every variant. */
export function nextSidebarSessionId(
  currentId: null | string,
  direction: 1 | -1,
  anySession: boolean,
  fallbackToAny = false
): null | string {
  const order = sidebarSessionOrder()

  const notBlocked = (id: string) => !isSessionParked(id)

  if (anySession) {
    return stepSessionId(order, currentId, direction, notBlocked)
  }

  const striped = stepSessionId(order, currentId, direction, sessionHasStatusStripe)

  return striped ?? (fallbackToAny ? stepSessionId(order, currentId, direction, notBlocked) : null)
}
