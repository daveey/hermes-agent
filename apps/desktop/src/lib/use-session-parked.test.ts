import { useStore } from '@nanostores/react'
import { act, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { $sessions, setSessions } from '@/store/session'
import { $muteWriteGuard, MUTE_WRITE_GUARD_MS, parkRowFor, writeSessionMuted } from '@/store/session-blocked'
import type { SessionInfo } from '@/types/hermes'

afterEach(() => vi.useRealTimers())

import { useSessionParked } from './use-session-parked'

it('keeps the stale-page guard after a rendered listed row acknowledges the write', async () => {
  vi.useFakeTimers()
  const row = { id: 'listed', muted: false, profile: 'default' } as SessionInfo
  setSessions([row])

  const { result } = renderHook(() => {
    const rows = useStore($sessions)

    return useSessionParked(row.id, rows[0])
  })

  await act(async () => writeSessionMuted(row, true, async () => ({})))
  expect(result.current).toBe(true)
  expect($muteWriteGuard.get().has(row.id)).toBe(true)
  act(() => setSessions([{ ...row }]))
  expect(result.current).toBe(true)
  await act(async () => vi.advanceTimersByTimeAsync(MUTE_WRITE_GUARD_MS))
  act(() => setSessions([{ ...row, muted: true }]))
  expect(result.current).toBe(true)
  expect($muteWriteGuard.get().has(row.id)).toBe(false)
})

it('remembers the drawn row and refreshes its owner and muted state', () => {
  const row = { id: 'drawn-only', connection_id: 'remote', profile: 'work', muted: false }

  const { rerender } = renderHook(({ entry }) => useSessionParked(entry.id, entry), {
    initialProps: { entry: row }
  })

  expect(parkRowFor(row.id)).toEqual(row)
  const refreshed = { ...row, muted: true, profile: 'other' }
  rerender({ entry: refreshed })
  expect(parkRowFor(row.id)).toEqual(refreshed)
})
