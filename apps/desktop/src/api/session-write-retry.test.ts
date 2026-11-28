import { beforeEach, expect, it, vi } from 'vitest'

import type * as ClientApi from './client'

vi.mock('@/lib/legacy-session-owner-backfill', () => ({ maybeBackfillLegacySessionOwners: vi.fn() }))
vi.mock('@/store/transcript-tail', () => ({ recordTranscriptTail: vi.fn() }))
vi.mock('@/app/session/hooks/use-session-actions/archive-queue-lifetime', () => ({
  archiveQueueLifetime: () => ({
    run: (run: () => Promise<unknown>) => run(),
    enqueue: vi.fn(),
    sleep: async () => {},
    dispose: vi.fn()
  })
}))
vi.mock('./client', async importOriginal => ({
  ...(await importOriginal<typeof ClientApi>()),
  hermesApi: vi.fn()
}))
import { archiveDrainWrite } from '@/app/session/hooks/use-session-actions/archive-drain-write'
import { sessionWriteRetryAction } from '@/lib/session-write-retry'

import { hermesApi, setApiRequestConnection, setApiRequestProfile } from './client'
import { pinSessionWriteOwner, renameSession, setSessionArchived } from './sessions'

// The Nov 11 hub refusal, verbatim in shape: a live owner that missed its identify deadlines.
const STALLED = new Error(
  '503: {"detail":"owner_unavailable: control_timeout: the gateway that owns this profile did not answer in time; nothing was changed, retry"}'
)

beforeEach(() => {
  vi.mocked(hermesApi).mockReset()
  setApiRequestConnection('owner')
  setApiRequestProfile('work')
})

function owner(revision: number, failures: Error[]) {
  const writes: Array<{ body: Record<string, unknown>; connectionId?: string; path: string }> = []
  let reads = 0
  vi.mocked(hermesApi).mockImplementation(async request => {
    if (!request.method || request.method === 'GET') {
      reads += 1

      return { exists: true, runtime_revision: revision, runtime_generation: 3 } as never
    }

    writes.push(request as never)
    const failure = failures.shift()

    if (failure) {
      throw failure
    }

    return { ok: true, revision: revision + 1 } as never
  })

  return { reads: () => reads, writes }
}

it('pins a write to the connection and profile it was first sent to', () => {
  expect(pinSessionWriteOwner('alt')).toEqual({ connectionId: 'owner', profile: 'alt' })
  expect(pinSessionWriteOwner(undefined)).toEqual({ connectionId: 'owner', profile: 'work' })
  expect(pinSessionWriteOwner({ connectionId: 'mini', profile: 'p' })).toEqual({ connectionId: 'mini', profile: 'p' })
  setApiRequestConnection(null)
  expect(pinSessionWriteOwner('alt')).toEqual({ profile: 'alt' })
})

it('a Retry after an owner stall replays the same fenced archive on the same owner', async () => {
  const seen = owner(4, [STALLED])
  const pinned = pinSessionWriteOwner('work')
  await expect(setSessionArchived('chat', true, pinned)).rejects.toBe(STALLED)
  // The window moved on to another gateway before the user clicked Retry.
  setApiRequestConnection('unrelated')
  setApiRequestProfile('other')
  await setSessionArchived('chat', true, pinned)
  expect(seen.writes).toHaveLength(2)
  expect(seen.reads()).toBe(1) // no fresh precondition for a write the owner may already hold
  expect(seen.writes[1]).toEqual(seen.writes[0])
  expect(seen.writes[0].connectionId).toBe('owner')
  expect(seen.writes[0].body).toMatchObject({
    archived: true,
    pinned: false,
    profile: 'work',
    expected_revision: 4,
    expected_generation: 3,
    request_id: expect.any(String)
  })
})

it('a drain retries the real fenced PATCH with unchanged identity and no new snapshot', async () => {
  const seen = owner(4, [new Error('503: {"detail":"owner_unavailable: draining: Exclusive maintenance refused"}')])
  const pinned = pinSessionWriteOwner('work')
  await archiveDrainWrite('drain-chat', {} as never, () => setSessionArchived('drain-chat', true, pinned))
  expect(seen.writes).toHaveLength(2)
  expect(seen.reads()).toBe(1)
  expect(seen.writes[1]).toEqual(seen.writes[0])
  expect(seen.writes[0].body).toMatchObject({ archived: true, request_id: expect.any(String), expected_revision: 4 })
})

it('a selected row rename is fenced on its own gateway', async () => {
  const seen = owner(9, [STALLED])
  const row = { connectionId: 'mini', profile: 'work' }
  await expect(renameSession('chat', 'New', row)).rejects.toBe(STALLED)
  await renameSession('chat', 'New', row)
  expect(seen.writes.map(write => write.connectionId)).toEqual(['mini', 'mini'])
  expect(seen.writes[1]).toEqual(seen.writes[0])
  expect(seen.writes[0].body).toMatchObject({
    title: 'New',
    profile: 'work',
    expected_revision: 9,
    request_id: expect.any(String)
  })
})

it('offers Retry exactly while the intent is still retained', async () => {
  expect(sessionWriteRetryAction(STALLED, 'Retry', () => {})?.label).toBeTruthy()
  expect(sessionWriteRetryAction(new Error('503: owner_unreachable'), 'Retry', () => {})).toBeDefined()
  expect(sessionWriteRetryAction(new Error('request timed out'), 'Retry', () => {})).toBeDefined()

  // A definitive refusal drops the identity, so the next try would be a NEW edit: no button.
  const conflict = new Error('409: {"detail":"revision_conflict"}')
  const seen = owner(2, [conflict])
  await expect(setSessionArchived('chat', true, 'work')).rejects.toBe(conflict)
  expect(sessionWriteRetryAction(conflict, 'Retry', () => {})).toBeUndefined()
  await setSessionArchived('chat', true, 'work')
  expect(seen.reads()).toBe(2)
  expect(seen.writes[1].body.request_id).not.toBe(seen.writes[0].body.request_id)
})
