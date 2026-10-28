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
  let settled = false

  // An unknown verdict reaches the user as "Task status unavailable". Record
  // why (owner, stage, error) in desktop.log: an expected 404 is never logged
  // by hermes:api, so without this line the dialog leaves no trace at all.
  const unknownBecause = (reason: string, error?: unknown): SessionInfo => {
    if (!settled) {
      settled = true
      const detail = error === undefined ? '' : `: ${String((error as Error)?.message ?? error).slice(0, 200)}`
      const route = `${owner.connectionId ?? '(none)'}/${owner.profile}${session?.connection_id ? '' : ' (no row owner)'}`
      console.error(`[archive-preflight] task status unknown for ${id} via ${route}: ${reason}${detail}`)
    }

    return unknown
  }

  if (!owner.connectionId) {return unknownBecause('no owner connection')}
  let timer: ReturnType<typeof setTimeout> | undefined
  let stage = 'start'

  const read = async (): Promise<SessionInfo> => {
    const ids = new Set<string>()
    let current: string | null = id

    for (let hop = 0; current && hop < 12; hop++) {
      if (ids.has(current)) {return unknownBecause(`ancestry cycle at ${current}`)}
      ids.add(current)
      stage = `GET /api/sessions/${current}`

      const row: { id?: string; parent_session_id?: string | null } = await hermesApiAs(owner, {
        path: `/api/sessions/${encodeURIComponent(current)}`, timeoutMs: 2_000
      })

      if (row?.id !== current || (row.parent_session_id != null && typeof row.parent_session_id !== 'string')) {
        return unknownBecause(`malformed session row from ${stage}`)
      }

      current = row.parent_session_id || null
    }

    if (current) {return unknownBecause('ancestry deeper than 12 hops')}
    stage = 'GET /api/session-kanban-tasks'

    const result = await hermesApiAs<{ tasks?: Array<{ id?: string; status?: string; stale?: boolean; unfinished?: boolean }> }>(owner, {
      path: `/api/session-kanban-tasks?ids=${encodeURIComponent([...ids].join(','))}`, timeoutMs: 2_000
    })

    if (!Array.isArray(result?.tasks)) {return unknownBecause(`malformed response from ${stage}`)}
    let active = 0
    let blocked = 0

    for (const task of result.tasks) {
      if (!task?.id || task.stale || !task.status) {return unknownBecause(`stale or incomplete task ${task?.id ?? '?'}`)}

      if (task.status === 'blocked') {blocked++}
      else if (['todo', 'ready', 'running', 'scheduled', 'review', 'triage'].includes(task.status)) {active++}
      else {return unknownBecause(`task ${task.id} has status ${task.status}`)}
    }

    settled = true

    return { ...session, kanban_active: active, kanban_blocked: blocked, kanban_unknown: false,
      kanban_remote_stale: 0, kanban_observed_at: Date.now() / 1000 } as SessionInfo
  }

  try {
    return await Promise.race([read(), new Promise<SessionInfo>(resolve => {
      timer = setTimeout(() => resolve(unknownBecause(`no answer within 2500ms (${stage})`)), 2_500)
    })])
  } catch (error) {
    return unknownBecause(`${stage} failed`, error)
  } finally {
    clearTimeout(timer)
  }
}
