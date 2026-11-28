import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { $kanbanObservationTick } from '@/store/kanban-observation'

import { useExecutionObservationClock } from './use-execution-observation-clock'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

it('keeps a stable clock sample until the existing tick or source row changes', () => {
  vi.useFakeTimers()
  vi.setSystemTime(1001_000)
  const initialRow = {}
  const { result, rerender } = renderHook(({ row }) => useExecutionObservationClock(row), {
    initialProps: { row: initialRow }
  })

  expect(result.current).toBe(1001)
  vi.setSystemTime(1002_000)
  rerender({ row: initialRow })
  expect(result.current).toBe(1001)
  rerender({ row: {} })
  expect(result.current).toBe(1002)
  act(() => {
    vi.setSystemTime(1011_000)
    $kanbanObservationTick.set($kanbanObservationTick.get() + 1)
  })
  expect(result.current).toBe(1011)
})
