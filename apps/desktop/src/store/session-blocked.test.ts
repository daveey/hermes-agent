import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

vi.mock('./notifications', () => ({ notifyError: vi.fn() }))
vi.mock('@/hermes', () => ({ setSessionMutedRemote: vi.fn() }))

import { setSessionMutedRemote } from '@/hermes'

import { notifyError } from './notifications'
import { $sessions, setSessions } from './session'
import {
  $blockedSessionIds,
  $mutedMigrationDone,
  $muteWriteGuard,
  isSessionParked,
  migrateLocalParks,
  MUTE_WRITE_GUARD_MS,
  resolveSessionParked,
  toggleSessionBlocked,
  writeSessionMuted
} from './session-blocked'

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
