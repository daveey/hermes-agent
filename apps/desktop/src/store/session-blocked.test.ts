import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

vi.mock('./notifications', () => ({ notifyError: vi.fn() }))
vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  setSessionMutedRemote: vi.fn()
}))

import { setSessionMutedRemote } from '@/hermes'

import { notifyError } from './notifications'
import { $sessions, setSessions } from './session'
import {
  $blockedSessionIds,
  $mutedMigrationDone,
  $muteWriteGuard,
  isSessionParked,
  migrateLocalParks,
  MUTE_ACK_HOLD_MS,
  MUTE_WRITE_GUARD_MS,
  parkRowFor,
  rememberParkRow,
  resolveSessionParked,
  toggleSessionBlocked,
  withRowMuted,
  writeSessionMuted
} from './session-blocked'
import { sessionStatusStripe } from './session-dot-state'

const row = (overrides: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id: 's1', message_count: 3, profile: 'default', ...overrides }) as unknown as SessionInfo

const mutedOf = (id: string) => $sessions.get().find(entry => entry.id === id)?.muted

beforeEach(() => {
  $blockedSessionIds.set([])
  $muteWriteGuard.set(new Map())
  setSessions([])
  vi.mocked(notifyError).mockClear()
  vi.mocked(setSessionMutedRemote).mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('resolveSessionParked', () => {
  it('reads row.muted when the backend stamps it', () => {
    expect(resolveSessionParked(false, { muted: true }, undefined)).toBe(true)
    // The backend is the authority: a stale local entry does not park an unmuted row.
    expect(resolveSessionParked(true, { muted: false }, undefined)).toBe(false)
  })

  it('falls back to the local ⌘B list when the row has no muted (older backend)', () => {
    expect(resolveSessionParked(true, {}, undefined)).toBe(true)
    expect(resolveSessionParked(false, undefined, undefined)).toBe(false)
  })

  it('a fresh write outranks the row; a stale one does not', () => {
    const now = 1_000_000

    expect(resolveSessionParked(false, { muted: false }, { at: now - 1, value: true }, now)).toBe(true)
    expect(resolveSessionParked(false, { muted: false }, { at: now - MUTE_WRITE_GUARD_MS - 1, value: true }, now)).toBe(
      false
    )
  })
})

describe('⌘B mute write-through', () => {
  it('success: paints at once, POSTs the new value, and stamps row.muted', async () => {
    setSessions([row({ muted: false })])
    let resolvePost!: (value: { ok: boolean; muted?: boolean }) => void
    vi.mocked(setSessionMutedRemote).mockImplementation(() => new Promise(resolve => (resolvePost = resolve)))

    expect(toggleSessionBlocked('s1')).toBe(true)

    // Optimistic: parked before the backend answers, even though row.muted still says false.
    expect(isSessionParked('s1')).toBe(true)
    expect($blockedSessionIds.get()).toEqual(['s1'])
    expect(setSessionMutedRemote).toHaveBeenCalledWith(expect.objectContaining({ id: 's1' }), true)

    resolvePost({ ok: true, muted: true })
    await vi.waitFor(() => expect(mutedOf('s1')).toBe(true))
    expect(notifyError).not.toHaveBeenCalled()

    // Toggling again un-parks through the same path.
    vi.mocked(setSessionMutedRemote).mockResolvedValue({ ok: true, muted: false })
    expect(toggleSessionBlocked('s1')).toBe(false)
    expect(setSessionMutedRemote).toHaveBeenLastCalledWith(expect.objectContaining({ id: 's1' }), false)
    await vi.waitFor(() => expect(mutedOf('s1')).toBe(false))
    expect(isSessionParked('s1')).toBe(false)
    expect($blockedSessionIds.get()).toEqual([])
  })

  it('failure: reverts the optimistic park and shows the error toast', async () => {
    setSessions([row({ muted: false })])
    const error = new Error('500 Internal Server Error')
    vi.mocked(setSessionMutedRemote).mockRejectedValue(error)

    await writeSessionMuted(row({ muted: false }), true)

    expect(isSessionParked('s1')).toBe(false)
    expect($blockedSessionIds.get()).toEqual([])
    expect($muteWriteGuard.get().size).toBe(0)
    expect(mutedOf('s1')).toBe(false)
    expect(notifyError).toHaveBeenCalledWith(error, 'Could not update the parked state')
  })

  it('failure on un-park: the row stays parked', async () => {
    setSessions([row({ muted: true })])
    $blockedSessionIds.set(['s1'])
    vi.mocked(setSessionMutedRemote).mockRejectedValue(new Error('network down'))

    await writeSessionMuted(row({ muted: true }), false)

    expect(isSessionParked('s1')).toBe(true)
    expect($blockedSessionIds.get()).toEqual(['s1'])
    expect(notifyError).toHaveBeenCalledTimes(1)
  })

  it('an older backend without the endpoint keeps the local-only park, silently', async () => {
    setSessions([row()])
    vi.mocked(setSessionMutedRemote).mockRejectedValue(new Error('404 Not Found'))

    await writeSessionMuted(row(), true)

    expect(isSessionParked('s1')).toBe(true)
    expect($blockedSessionIds.get()).toEqual(['s1'])
    expect(notifyError).not.toHaveBeenCalled()
  })

  it('a draft with no persisted row stays local and never POSTs', () => {
    expect(toggleSessionBlocked('draft-1')).toBe(true)
    expect($blockedSessionIds.get()).toEqual(['draft-1'])
    expect(setSessionMutedRemote).not.toHaveBeenCalled()
  })
})

describe('drawn rows outside the recent list', () => {
  beforeEach(() => vi.useFakeTimers())

  it('POSTs to the remembered owner and holds the ack over a stale drawn row', async () => {
    const old = row({ id: 'old', muted: false, connection_id: 'remote', profile: 'work' })
    rememberParkRow(old)
    vi.mocked(setSessionMutedRemote).mockResolvedValue({ ok: true, muted: true })
    expect(toggleSessionBlocked('old')).toBe(true)
    await Promise.resolve()
    expect(setSessionMutedRemote).toHaveBeenCalledWith(old, true)
    expect($sessions.get()).toEqual([])
    await vi.advanceTimersByTimeAsync(MUTE_WRITE_GUARD_MS + 1)
    expect(isSessionParked('old', old)).toBe(true)
    expect(toggleSessionBlocked('old')).toBe(false)
    expect(setSessionMutedRemote).toHaveBeenLastCalledWith(old, false)
    await Promise.resolve()
  })

  it('releases an acknowledged hold when a drawn row agrees', async () => {
    const old = row({ muted: false })
    rememberParkRow(old)
    await writeSessionMuted(old, true, async () => ({}))
    rememberParkRow({ ...old, muted: true })
    expect($muteWriteGuard.get().has(old.id)).toBe(false)
    expect(isSessionParked(old.id, { muted: false })).toBe(false)
  })

  it('expires an acknowledged hold after ten minutes', async () => {
    const old = row({ muted: false })
    await writeSessionMuted(old, true, async () => ({}))
    await vi.advanceTimersByTimeAsync(MUTE_ACK_HOLD_MS - 1)
    expect(isSessionParked(old.id, old)).toBe(true)
    await vi.advanceTimersByTimeAsync(1)
    expect(isSessionParked(old.id, old)).toBe(false)
  })

  it('rolls back and toasts when a remembered row POST fails', async () => {
    const old = row({ muted: false })
    rememberParkRow(old)
    const error = new Error('offline')
    vi.mocked(setSessionMutedRemote).mockRejectedValue(error)
    toggleSessionBlocked(old.id)
    await Promise.resolve()
    expect(isSessionParked(old.id, old)).toBe(false)
    expect($muteWriteGuard.get().size).toBe(0)
    expect(notifyError).toHaveBeenCalledWith(error, 'Could not update the parked state')
  })

  it('bounds the drawn-row registry and refreshes remembered entries', () => {
    for (let i = 0; i < 1000; i++) {
      rememberParkRow(row({ id: `old-${i}` }))
    }

    rememberParkRow(row({ id: 'old-0', profile: 'updated' }))
    rememberParkRow(row({ id: 'newest' }))
    expect(parkRowFor('old-1')).toBeUndefined()
    expect(parkRowFor('old-0')?.profile).toBe('updated')
    expect(parkRowFor('newest')?.id).toBe('newest')
  })

  it('resolves lineage aliases even when neither id is in the recent list', async () => {
    const tip = row({ id: 'tip', _lineage_root_id: 'root', muted: false })
    rememberParkRow(tip)
    vi.mocked(setSessionMutedRemote).mockResolvedValue({ ok: true, muted: true })
    expect(toggleSessionBlocked('root')).toBe(true)
    await Promise.resolve()
    expect(setSessionMutedRemote).toHaveBeenCalledWith(tip, true)
    expect(isSessionParked('root', tip)).toBe(true)
    expect(toggleSessionBlocked('root')).toBe(false)
    await Promise.resolve()
  })
})

describe('one-time migration of local ⌘B parks', () => {
  beforeEach(() => {
    $mutedMigrationDone.set([])
  })

  it('posts muted=true once for each locally parked row the backend says is unmuted', async () => {
    const rows = [row({ id: 'a', muted: false }), row({ id: 'b', muted: false }), row({ id: 'c', muted: true })]
    setSessions(rows)
    $blockedSessionIds.set(['a', 'c', 'gone'])
    vi.mocked(setSessionMutedRemote).mockResolvedValue({ ok: true, muted: true })

    await migrateLocalParks(rows)

    // Only `a`: `b` was never parked, `c` already is, `gone` has no row.
    expect(setSessionMutedRemote).toHaveBeenCalledTimes(1)
    expect(setSessionMutedRemote).toHaveBeenCalledWith(expect.objectContaining({ id: 'a' }), true)
    expect(mutedOf('a')).toBe(true)
    expect(isSessionParked('a')).toBe(true)
    expect($mutedMigrationDone.get()).toEqual(['primary\u0000default'])
  })

  it('does not re-run for a profile it already migrated', async () => {
    const rows = [row({ id: 'a', muted: false })]
    setSessions(rows)
    $blockedSessionIds.set(['a'])
    vi.mocked(setSessionMutedRemote).mockResolvedValue({ ok: true, muted: true })

    await migrateLocalParks(rows)
    await migrateLocalParks([row({ id: 'a', muted: false })])

    expect(setSessionMutedRemote).toHaveBeenCalledTimes(1)
  })

  it('waits for a backend that stamps muted: rows without it change nothing', async () => {
    const rows = [row({ id: 'a' })]
    setSessions(rows)
    $blockedSessionIds.set(['a'])

    await migrateLocalParks(rows)

    expect(setSessionMutedRemote).not.toHaveBeenCalled()
    expect($mutedMigrationDone.get()).toEqual([])
  })

  it('a 404 leaves the local list alone and does not retry', async () => {
    const rows = [row({ id: 'a', muted: false })]
    setSessions(rows)
    $blockedSessionIds.set(['a'])
    vi.mocked(setSessionMutedRemote).mockRejectedValue(new Error('404 Not Found'))

    await migrateLocalParks(rows)

    expect($blockedSessionIds.get()).toEqual(['a'])
    expect(mutedOf('a')).toBe(false)
    expect(notifyError).not.toHaveBeenCalled()
    expect($mutedMigrationDone.get()).toEqual(['primary\u0000default'])
  })

  it('any other failure leaves the profile unmarked so the next load retries', async () => {
    const rows = [row({ id: 'a', muted: false })]
    setSessions(rows)
    $blockedSessionIds.set(['a'])
    vi.mocked(setSessionMutedRemote).mockRejectedValueOnce(new Error('network down'))

    await migrateLocalParks(rows)
    expect($mutedMigrationDone.get()).toEqual([])

    vi.mocked(setSessionMutedRemote).mockResolvedValue({ ok: true, muted: true })
    await migrateLocalParks(rows)
    expect(setSessionMutedRemote).toHaveBeenCalledTimes(2)
    expect($mutedMigrationDone.get()).toEqual(['primary\u0000default'])
  })
})

describe('the row after a park write', () => {
  const paint = (r: SessionInfo) => sessionStatusStripe('idle', true, false, 0, false, null, 0, 0, 0, r.outcome)

  it('unmuting drops a stale `muted` outcome so the stripe falls back to the derived logic', async () => {
    setSessions([row({ muted: true, outcome: { kind: 'muted' } })])
    $blockedSessionIds.set(['s1'])
    vi.mocked(setSessionMutedRemote).mockResolvedValue({ ok: true, muted: false })

    expect(toggleSessionBlocked('s1')).toBe(false)
    await vi.waitFor(() => expect(mutedOf('s1')).toBe(false))

    const after = $sessions.get()[0]
    expect(after.outcome).toBeUndefined()
    expect(isSessionParked('s1')).toBe(false)
    expect(paint(after)).toBe('done')
  })

  it('muting stamps a `muted` outcome', async () => {
    setSessions([row({ muted: false, outcome: { kind: 'done', summary: 'shipped' } })])
    vi.mocked(setSessionMutedRemote).mockResolvedValue({ ok: true, muted: true })

    toggleSessionBlocked('s1')
    await vi.waitFor(() => expect(mutedOf('s1')).toBe(true))

    expect($sessions.get()[0].outcome?.kind).toBe('muted')
    expect(paint($sessions.get()[0])).toBe('blocked')
  })

  it('unmuting keeps any outcome other than `muted`', () => {
    const r = withRowMuted(row({ muted: true, outcome: { kind: 'decision', items: [] } }), false)

    expect(r.outcome?.kind).toBe('decision')
    expect(r.muted).toBe(false)
  })
})
