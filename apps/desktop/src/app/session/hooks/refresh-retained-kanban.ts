import type { SessionInfo } from '@/types/hermes'

export function withKanbanEvidence(row: SessionInfo, fresh: SessionInfo): SessionInfo {
  const next = { ...row }

  for (const key of Object.keys(next)) {
    if (key.startsWith('kanban_')) { delete (next as unknown as Record<string, unknown>)[key] }
  }

  for (const [key, value] of Object.entries(fresh)) {
    if (key.startsWith('kanban_')) { (next as unknown as Record<string, unknown>)[key] = value }
  }

  return next
}

/** Refresh task evidence for retained rows outside the recency window. Never
 * replace their transcript/optimistic metadata or borrow another owner's row. */
export async function refreshRetainedKanban(
  rows: SessionInfo[],
  read: (row: SessionInfo) => Promise<SessionInfo>
): Promise<SessionInfo[]> {
  const output = [...rows]
  let cursor = 0

  await Promise.all(Array.from({ length: Math.min(4, rows.length) }, async () => {
    while (cursor < rows.length) {
      const index = cursor++
      const row = rows[index]
      let timer: ReturnType<typeof setTimeout> | undefined

      try {
        const fresh = await Promise.race([
          read(row),
          new Promise<never>((_, reject) => {
            timer = setTimeout(() => reject(new Error('Task evidence timeout')), 5000)
          })
        ])

        if (fresh.id !== row.id || (fresh.profile ?? 'default') !== (row.profile ?? 'default')) {
          throw new Error('Task evidence owner mismatch')
        }

        // Older gateways return session details without task annotations. A
        // successful HTTP read is not proof of zero linked work.
        const stamp = fresh.kanban_observed_at
        const counts = [fresh.kanban_active, fresh.kanban_blocked, fresh.kanban_attention, fresh.kanban_remote_stale]

        if (typeof stamp !== 'number' || !Number.isFinite(stamp) || stamp <= 0 ||
          fresh.kanban_unknown || counts.some(value => value !== undefined &&
            (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0))) {
          throw new Error('Task evidence unavailable')
        }

        output[index] = withKanbanEvidence(row, fresh)
      } catch {
        output[index] = { ...row, kanban_unknown: true }
      } finally {
        clearTimeout(timer)
      }
    }
  }))

  return output
}
