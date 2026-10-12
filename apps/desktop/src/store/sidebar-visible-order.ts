import { $sessions } from '@/store/session'
import { $sessionDotStateById, type SessionDotState } from '@/store/session-dot-state'

// The sidebar's session order as the user sees it, for ⌘] / ⌘[ stepping.
// Each rendered SessionsSection (pinned, recents, …) publishes the ids of its
// visible rows plus an anchor element; sections are concatenated in DOM order
// at read time. The virtualized list renders only on-screen rows, so the DOM
// can't be read directly — the section's row model is the authority.

interface SectionEntry {
  anchor: HTMLElement | null
  ids: readonly string[]
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

/** Visible sidebar session ids, top to bottom (deduped). Falls back to the
 *  recency list when no section is mounted (sidebar hidden, project view). */
export function sidebarSessionOrder(): string[] {
  const ordered = [...sections.values()].filter(entry => entry.anchor?.isConnected).sort(domOrder)
  const seen = new Set<string>()
  const ids: string[] = []

  for (const entry of ordered) {
    for (const id of entry.ids) {
      if (!seen.has(id)) {
        seen.add(id)
        ids.push(id)
      }
    }
  }

  return ids.length ? ids : $sessions.get().map(session => session.id)
}

/** The sidebar dot states that mean "waiting on you": a blocking prompt
 *  (amber) or a finished, unread turn. */
export const NEEDS_YOU_STATES: ReadonlySet<SessionDotState> = new Set(['needs-input', 'unread'])

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

/** ⌘] / ⌘[: next session that needs you, else the plain next one. ⇧ skips
 *  the needs-you filter. */
export function nextSidebarSessionId(currentId: null | string, direction: 1 | -1, anySession: boolean): null | string {
  const order = sidebarSessionOrder()

  if (!anySession) {
    const dots = $sessionDotStateById.get()
    const needing = stepSessionId(order, currentId, direction, id => NEEDS_YOU_STATES.has(dots[id] ?? 'idle'))

    if (needing) {
      return needing
    }
  }

  return stepSessionId(order, currentId, direction)
}
