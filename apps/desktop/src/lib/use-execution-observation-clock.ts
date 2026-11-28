import { useStore } from '@nanostores/react'
import { useMemo } from 'react'

import { $backgroundStatusBySession } from '@/store/composer-status'
import { $kanbanObservationTick } from '@/store/kanban-observation'
import { $liveWorkRoster } from '@/store/live-work-roster'
import { $subagentsBySession } from '@/store/subagents'

/** Explicit, stable render clock for evidence consumers (including off-page rows).
 * Re-sample on a source-row change or the existing tick, never during getSnapshot:
 * Date.now() there is unstable under real timers and can cause a render loop.
 * Passing this scalar to classifiers also makes expiry visible to React Compiler. */
export function useExecutionObservationClock(row: unknown): number {
  const tick = useStore($kanbanObservationTick)
  const roster = useStore($liveWorkRoster)
  const background = useStore($backgroundStatusBySession)
  const subagents = useStore($subagentsBySession)
  const sample = useMemo(
    () => ({ nowSeconds: Date.now() / 1000, row, tick, roster, background, subagents }),
    [row, tick, roster, background, subagents]
  )

  return sample.nowSeconds
}
