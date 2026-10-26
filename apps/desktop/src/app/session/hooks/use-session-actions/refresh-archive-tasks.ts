import { hermesApiAs } from '@/api/client'
import type { SessionInfo } from '@/hermes'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { hasFreshKanbanObservation } from '@/store/kanban-observation'

/** Expired sidebar evidence is a reason to read, not evidence of unfinished work.
 * Capture the row owner before awaiting; never borrow the foreground gateway.
 * Failed/malformed reads remain unknown, including incomplete ancestry. */
export async function refreshArchiveTasks(id: string, session: SessionInfo | undefined, profile: string | undefined): Promise<SessionInfo | undefined> {
  if (hasFreshKanbanObservation(session) && !session?.kanban_unknown && !session?.kanban_remote_stale) {
    return session
  }

  const owner = {
    connectionId: session?.connection_id || $connectionsRegistry.get()?.primary || null,
    profile: profile || session?.profile || 'default'
  }

  const unknown = { ...session, kanban_unknown: true } as SessionInfo

  if (!owner.connectionId) {return unknown}
  let timer: ReturnType<typeof setTimeout> | undefined

  const read = async (): Promise<SessionInfo> => {
    const ids = new Set<string>()
    let current: string | null = id

    for (let hop = 0; current && hop < 12; hop++) {
      if (ids.has(current)) {return unknown}
      ids.add(current)

      const row: { id?: string; parent_session_id?: string | null } = await hermesApiAs(owner, {
        path: `/api/sessions/${encodeURIComponent(current)}`, timeoutMs: 2_000
      })

      if (row?.id !== current || (row.parent_session_id != null && typeof row.parent_session_id !== 'string')) {return unknown}
      current = row.parent_session_id || null
    }

    if (current) {return unknown}

    const result = await hermesApiAs<{ tasks?: Array<{ id?: string; status?: string; stale?: boolean; unfinished?: boolean }> }>(owner, {
      path: `/api/session-kanban-tasks?ids=${encodeURIComponent([...ids].join(','))}`, timeoutMs: 2_000
    })

    if (!Array.isArray(result?.tasks)) {return unknown}
    let active = 0
    let blocked = 0

    for (const task of result.tasks) {
      if (!task?.id || task.stale || !task.status) {return unknown}

      if (task.status === 'blocked') {blocked++}
      else if (['todo', 'ready', 'running', 'scheduled', 'review', 'triage'].includes(task.status)) {active++}
      else {return unknown}
    }

    return { ...session, kanban_active: active, kanban_blocked: blocked, kanban_unknown: false,
      kanban_remote_stale: 0, kanban_observed_at: Date.now() / 1000 } as SessionInfo
  }

  try {
    return await Promise.race([read(), new Promise<SessionInfo>(resolve => {
      timer = setTimeout(() => resolve(unknown), 2_500)
    })])
  } catch {
    return unknown
  } finally {
    clearTimeout(timer)
  }
}
