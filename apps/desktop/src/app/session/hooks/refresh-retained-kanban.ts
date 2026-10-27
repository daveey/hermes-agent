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
