import { useStore } from '@nanostores/react'
import { useMemo } from 'react'

import { $kanbanObservationTick } from '@/store/kanban-observation'

/** Explicit, stable render clock for evidence consumers (including off-page rows).
 * Re-sample on a source-row change or the existing tick, never during getSnapshot:
 * Date.now() there is unstable under real timers and can cause a render loop.
 * Passing this scalar to classifiers also makes expiry visible to React Compiler. */
export function useExecutionObservationClock(row: unknown): number {
  const tick = useStore($kanbanObservationTick)
  const sample = useMemo(() => ({ nowSeconds: Date.now() / 1000, row, tick }), [row, tick])

  return sample.nowSeconds
}
