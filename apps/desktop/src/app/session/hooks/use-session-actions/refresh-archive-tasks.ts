import { hermesApiAs } from '@/api/client'
import { isReconnectableSessionWriteError } from '@/api/session-write-reconnect'
import type { SessionInfo } from '@/hermes'
import type { ArchiveTrace } from '@/lib/archive-trace'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { hasFreshKanbanObservation } from '@/store/kanban-observation'

/** A busy backend can legitimately take several seconds to answer; "unknown" must mean the read
 * failed, not that it was slow. Each read gets a generous timeout and one retry on a timeout, all
 * inside an overall budget so the dialog never hangs. */
const READ_TIMEOUT_MS = 6_000
const OVERALL_BUDGET_MS = 14_000

const isTimeout = (error: unknown) => /timed out/i.test(String((error as Error)?.message ?? error))

/** Expired sidebar evidence is a reason to read, not evidence of unfinished work.
 * Capture the row owner before awaiting; never borrow the foreground gateway.
 * Failed/malformed reads remain unknown, including incomplete ancestry.
 * A fresh row that claims work is read too: its `kanban_active` also counts settled cards still waiting for the chat's
 * close-out, which the task list leaves out, so only the read says what is open. If that read fails, the row stands. */
export async function refreshArchiveTasks(id: string, session: SessionInfo | undefined, profile: string | undefined,
  options?: { retryTransport?: boolean; budgetMs?: number; trace?: ArchiveTrace }): Promise<SessionInfo | undefined> {
  const fresh = hasFreshKanbanObservation(session) && !session?.kanban_unknown && !session?.kanban_remote_stale

  if (fresh && !session?.kanban_active && !session?.kanban_blocked) {
    options?.trace?.('preflight=ok')

    return session
  }

  const owner = {
    connectionId: session?.connection_id || $connectionsRegistry.get()?.primary || null,
    profile: profile || session?.profile || 'default'
  }

  const unknown = (fresh ? session : { ...session, kanban_unknown: true }) as SessionInfo
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
      options?.trace?.(`preflight=unknown:${reason}${detail}`)
    }

    return unknown
  }

  if (!owner.connectionId) {return unknownBecause('no owner connection')}
  let timer: ReturnType<typeof setTimeout> | undefined
  let stage = 'start'

  const request = async <T>(path: string): Promise<T> => {
    try {
      return await hermesApiAs<T>(owner, { path, timeoutMs: Math.min(READ_TIMEOUT_MS, options?.budgetMs ?? READ_TIMEOUT_MS) })
    } catch (error) {
      if (options?.retryTransport && isReconnectableSessionWriteError(error)) {throw error}

      if (!isTimeout(error)) {throw error}

      return hermesApiAs<T>(owner, { path, timeoutMs: READ_TIMEOUT_MS })
    }
  }

  const read = async (): Promise<SessionInfo> => {
    const ids = new Set<string>()
    let current: string | null = id

    for (let hop = 0; current && hop < 12; hop++) {
      if (ids.has(current)) {return unknownBecause(`ancestry cycle at ${current}`)}
      ids.add(current)
      stage = `GET /api/sessions/${current}`

      const row: { id?: string; parent_session_id?: string | null } = await request(`/api/sessions/${encodeURIComponent(current)}`)

      if (row?.id !== current || (row.parent_session_id != null && typeof row.parent_session_id !== 'string')) {
        return unknownBecause(`malformed session row from ${stage}`)
      }

      current = row.parent_session_id || null
    }

    if (current) {return unknownBecause('ancestry deeper than 12 hops')}
    stage = 'GET /api/session-kanban-tasks'

    const result = await request<{ tasks?: Array<{ id?: string; status?: string; stale?: boolean; unfinished?: boolean }> }>(
      `/api/session-kanban-tasks?ids=${encodeURIComponent([...ids].join(','))}`
    )

    if (!Array.isArray(result?.tasks)) {return unknownBecause(`malformed response from ${stage}`)}
    let active = 0
    let blocked = 0

    for (const task of result.tasks) {
      if (!task?.id || task.stale || !task.status) {return unknownBecause(`stale or incomplete task ${task?.id ?? '?'}`)}

      if (task.status === 'blocked') {blocked++}
      else if (['todo', 'ready', 'running', 'scheduled', 'review', 'triage'].includes(task.status)) {active++}
      else {return unknownBecause(`task ${task.id} has status ${task.status}`)}
    }

    if (settled) {return unknown}
    settled = true
    options?.trace?.('preflight=ok')

    return { ...session, kanban_active: active, kanban_blocked: blocked, kanban_unknown: false,
      kanban_remote_stale: 0, kanban_observed_at: Date.now() / 1000 } as SessionInfo
  }

  try {
    return await Promise.race([read(), new Promise<SessionInfo>(resolve => {
      timer = setTimeout(() => resolve(unknownBecause(`no answer within ${OVERALL_BUDGET_MS}ms (${stage})`)), Math.min(OVERALL_BUDGET_MS, options?.budgetMs ?? OVERALL_BUDGET_MS))
    })])
  } catch (error) {
    if (options?.retryTransport && isReconnectableSessionWriteError(error)) {throw error}

    return unknownBecause(`${stage} failed`, error)
  } finally {
    clearTimeout(timer)
  }
}
