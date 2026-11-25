import { atom } from 'nanostores'

import { registry } from '@/contrib/registry'
import type { Contribution } from '@/contrib/types'
import type { SessionInfo } from '@/hermes'

// Session-list filters — the `sessions.filter` registry area. A plugin (the
// lanes strip) narrows the desktop session list by CONTRIBUTING a predicate;
// core still owns rendering and ANDs every contribution with its own filters.
// Same shape as `sidebarNav.prefs` (./sidebar-nav.ts): a contribution is
// attributed and dropped by the loader's per-plugin disposer on disable /
// reload, so the rows come back on their own.

export const SESSION_FILTER_AREA = 'sessions.filter'

/** Payload (`data`) of a `sessions.filter` contribution. */
export interface SessionFilterContribution {
  /** Whether the session stays listed. Contributions AND together. A `match`
   *  that is not a function or throws counts as matching (fail-open). */
  match: (session: SessionInfo) => boolean
  /** Human label for the filter (reserved for the filter indicator). */
  label?: string
  /** Called when the user clears the sidebar filters, so the plugin can drop
   *  its own selection. Errors are swallowed. */
  onClear?: () => void
}

const warned = new Set<string>()

function warnOnce(id: string, reason: string, error?: unknown): void {
  if (warned.has(id)) {
    return
  }

  warned.add(id)
  console.warn(`[sessions.filter] contribution "${id}" ${reason}; treating it as matching`, error ?? '')
}

/** AND over every contribution's `data.match`. Pure. An empty list matches
 *  everything; a contribution whose `match` is not a function or throws counts
 *  as matching (console.warn once per contribution id). */
export function applySessionFilters(session: SessionInfo, contributions: readonly Contribution[]): boolean {
  for (const c of contributions) {
    const match = (c.data as Partial<SessionFilterContribution> | undefined)?.match

    if (typeof match !== 'function') {
      warnOnce(c.id, '`match` is not a function')

      continue
    }

    try {
      if (!match(session)) {
        return false
      }
    } catch (error) {
      warnOnce(c.id, '`match` threw', error)
    }
  }

  return true
}

/** Whether the session has any open kanban card: the fields the row's card
 *  squares read (session-row-card-squares.tsx). */
export function sessionHasOpenCards(session: SessionInfo): boolean {
  return (
    (session.kanban_active ?? 0) > 0 ||
    (session.kanban_blocked ?? 0) > 0 ||
    (session.kanban_held ?? 0) > 0 ||
    (session.kanban_parked ?? 0) > 0 ||
    (session.kanban_stalled ?? 0) > 0 ||
    (session.needs_you?.length ?? 0) > 0
  )
}

/** Run every contribution's `onClear` (each in its own try/catch). */
export function clearSessionFilterContributions(): void {
  for (const c of registry.getArea(SESSION_FILTER_AREA)) {
    const onClear = (c.data as Partial<SessionFilterContribution> | undefined)?.onClear

    if (typeof onClear !== 'function') {
      continue
    }

    try {
      onClear()
    } catch (error) {
      console.warn(`[sessions.filter] contribution "${c.id}" onClear threw`, error)
    }
  }
}

/** The sidebar's session list with core filters applied but WITHOUT plugin
 *  session filters. Published by ChatSidebar while mounted; read via the SDK's
 *  `useSidebarSessions`. */
export const $sidebarSessionsPrePlugin = atom<readonly SessionInfo[]>([])
