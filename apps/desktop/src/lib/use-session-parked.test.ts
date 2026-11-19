import { renderHook } from '@testing-library/react'
import { expect, it } from 'vitest'

import { parkRowFor } from '@/store/session-blocked'

import { useSessionParked } from './use-session-parked'

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
