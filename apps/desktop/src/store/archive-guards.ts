// [daveey] Archive guards: things still linked to a chat (open Kanban tasks,
// rented machines, …) that the user should see before the chat is archived.
// Archiving only hides the chat; the linked work keeps running (and billing)
// out of sight. `archiveSession` collects every guard's findings and asks for
// confirmation when any come back.
//
// Core contributes the Kanban guard from the row's own counts (the same ones
// the sidebar badge shows). Plugins add theirs through
// `ctx.registerArchiveGuard` (e.g. session-tasks reports rented machines from
// compute-rentals). A guard that throws or outlives the deadline is skipped,
// so a slow or broken plugin can delay archiving by at most the timeout and
// never blocks it.

import type { ArchiveTaskCopy } from '@/i18n/archive-task-copy'
import type { SessionInfo } from '@/types/hermes'

import { hasFreshKanbanObservation } from './kanban-observation'

/** One line in the confirm dialog: `label` = the kind ("Kanban tasks"),
 *  `value` = what's linked ("3 open, 1 blocked"). */
export interface ArchiveLinkedItem {
  label: string
  value: string
}

export interface ArchiveGuardContext {
  /** The stored id being archived (may be the compression tip). */
  sessionId: string
  /** The listed sidebar row, when the session is listed. */
  session?: SessionInfo
  /** Every id the chat is known under: the stored id, the row id and its
   *  lineage root. Guards that bind by session id should also walk
   *  `parent_session_id` if they need the full chain. */
  ids: string[]
  /** Owning profile, when known. */
  profile?: string
  /** Registry connection that owns the row; absent = primary/local gateway. */
  connectionId?: string
}

export type ArchiveGuard = (
  context: ArchiveGuardContext
) => ArchiveLinkedItem[] | null | Promise<ArchiveLinkedItem[] | null | undefined> | undefined

const guards = new Map<string, ArchiveGuard>()

/** Register a guard under a unique id; the returned disposer removes it. A
 *  second registration under the same id replaces the first. */
export function registerArchiveGuard(id: string, guard: ArchiveGuard): () => void {
  guards.set(id, guard)

  return () => {
    if (guards.get(id) === guard) {
      guards.delete(id)
    }
  }
}

export const ARCHIVE_GUARD_TIMEOUT_MS = 2_500

function validItems(value: unknown): ArchiveLinkedItem[] {
  if (!Array.isArray(value)) {
    return []
  }

  return value.filter(
    (item): item is ArchiveLinkedItem =>
      Boolean(item) &&
      typeof item.label === 'string' &&
      typeof item.value === 'string' &&
      item.label.trim() !== '' &&
      item.value.trim() !== ''
  )
}

/** Run every registered guard in parallel (each capped at `timeoutMs`) and
 *  return their findings in registration order. Never throws. */
export async function collectArchiveLinks(
  context: ArchiveGuardContext,
  timeoutMs = ARCHIVE_GUARD_TIMEOUT_MS
): Promise<ArchiveLinkedItem[]> {
  const runs = [...guards.values()].map(async guard => {
    let timer: ReturnType<typeof setTimeout> | undefined

    try {
      const deadline = new Promise<null>(resolve => {
        timer = setTimeout(() => resolve(null), timeoutMs)
      })

      return validItems(await Promise.race([Promise.resolve().then(() => guard(context)), deadline]))
    } catch {
      return []
    } finally {
      if (timer) {
        clearTimeout(timer)
      }
    }
  })

  return (await Promise.all(runs)).flat()
}

/** Core guard: the row's own Kanban counts (summed over its lineage by the
 *  backend, same numbers as the sidebar badge). `copy.value` renders the value
 *  so the caller owns the copy. */
export function sessionKanbanArchiveLinks(
  session: SessionInfo | undefined,
  copy: ArchiveTaskCopy
): ArchiveLinkedItem[] {
  return kanbanArchiveLinks(session, {
    label: copy.archiveLinkedTasksLabel,
    unknown: copy.archiveLinkedTasksUnknown,
    value: copy.archiveLinkedTasksValue
  })
}

export function kanbanArchiveLinks(
  session: SessionInfo | undefined,
  copy: { label: string; unknown: string; value: (open: number, blocked: number) => string }
): ArchiveLinkedItem[] {
  if (session?.kanban_unknown) {
    return [{ label: copy.label, value: copy.unknown }]
  }

  const open = Math.max(0, Number(session?.kanban_active) || 0)
  const blocked = Math.max(0, Number(session?.kanban_blocked) || 0)

  if (open + blocked === 0) {
    return hasFreshKanbanObservation(session) && !(session?.kanban_remote_stale ?? 0)
      ? []
      : [{ label: copy.label, value: copy.unknown }]
  }

  return [{ label: copy.label, value: copy.value(open, blocked) }]
}

/** Test seam. */
export function resetArchiveGuardsForTests(): void {
  guards.clear()
}
