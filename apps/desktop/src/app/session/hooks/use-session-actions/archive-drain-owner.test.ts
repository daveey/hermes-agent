import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const { api, capture, primary, enqueue, dispose } = vi.hoisted(() => ({ api: vi.fn(), capture: vi.fn(),
  primary: { value: 'hub', connections: ['hub'] }, enqueue: vi.fn(), dispose: vi.fn() }))

vi.mock('@/api/client', () => ({ hermesApiAs: api }))
vi.mock('@/store/session', () => ({ sessionMatchesStoredId: (row: { id: string }, id: string) => row.id === id }))
vi.mock('@/store/connection-registry-state', () => ({ $connectionsRegistry: { get: () => ({ primary: primary.value, connections: primary.connections.map(id => ({ id })) }) } }))
vi.mock('./archive-soft-hide', () => ({ captureArchiveOwner: capture }))
vi.mock('./archive-queue-lifetime', () => ({ archiveQueueLifetime: () => ({
  run: (run: () => Promise<unknown>) => run(), enqueue,
  sleep: async () => {}, dispose
}) }))

import type { SessionInfo } from '@/hermes'

import { captureArchiveOwnerAfterDrain } from './archive-drain-owner'

const unresolved = new Error('Session owner could not be resolved; select its row')
const row = { id: 'chat', connection_id: 'hub', profile: 'work' } as SessionInfo
beforeEach(() => { vi.resetAllMocks(); primary.value = 'hub'; capture.mockImplementation((_id, selected) => {
  if (selected) { return selected }
  throw unresolved
}) })
afterEach(() => { primary.value = 'hub'; primary.connections = ['hub'] })

it('holds unresolved ownership during list503, then uses the one proved row from the captured source', async () => {
  api.mockImplementationOnce(async () => { primary.value = 'other'; throw new Error('503: sessions projection unavailable') })
    .mockResolvedValueOnce({ sessions: [row], total: 1, offset: 0 })
  await expect(captureArchiveOwnerAfterDrain('chat', undefined, undefined, {} as never)).resolves.toEqual(row)
  expect(api.mock.calls.map(([scope]) => scope.connectionId)).toEqual(['hub', 'hub'])
  expect(enqueue).toHaveBeenCalledOnce()
  expect(dispose).toHaveBeenCalledOnce()
  expect(capture.mock.calls.at(-1)?.[1]).toEqual(row)
})

it.each([{ sessions: [] }, { sessions: [row, { ...row, profile: 'other' }] }])('never guesses from a successful list with zero or multiple owners: $sessions.length', async ({ sessions }) => {
  api.mockResolvedValue({ sessions, total: sessions.length, offset: 0 })
  await expect(captureArchiveOwnerAfterDrain('chat', undefined, undefined, {} as never)).rejects.toBe(unresolved)
  expect(api).toHaveBeenCalledOnce()
  expect(enqueue).not.toHaveBeenCalled()
})

it('stamps untagged primary rows with the proving source, not a replacement primary', async () => {
  api.mockImplementationOnce(async () => { primary.value = 'other'; throw new Error('503: sessions projection unavailable') })
    .mockResolvedValueOnce({ sessions: [{ ...row, connection_id: undefined }], total: 1, offset: 0 })
  await expect(captureArchiveOwnerAfterDrain('chat', undefined, undefined, {} as never)).resolves.toEqual(row)
  expect(capture.mock.calls.at(-1)?.[1].connection_id).toBe('hub')
})

it.each([false, true])('reads beyond page one before proving a unique owner (duplicate beyond page: %s)', async duplicate => {
  const first = [row, ...Array.from({ length: 299 }, (_, n) => ({ ...row, id: `other-${n}` }))]
  api.mockResolvedValueOnce({ sessions: first, total: 301, offset: 0 })
    .mockResolvedValueOnce({ sessions: [{ ...row, id: duplicate ? 'chat' : 'last', profile: 'other' }], total: 301, offset: 300 })
  const result = captureArchiveOwnerAfterDrain('chat', undefined, undefined, {} as never)
  if (duplicate) { await expect(result).rejects.toBe(unresolved) }
  else { await expect(result).resolves.toEqual(row) }
  expect(api.mock.calls[1][1].path).toContain('offset=300')
})

it.each([
  { sessions: [row], total: 301, offset: 0 },
  { sessions: [row], total: 1, offset: 0, errors: [{ profile: 'other', error: 'unavailable' }] },
  { sessions: [row] },
  { sessions: [row], total: 1, offset: 300 }
])('refuses incomplete or malformed aggregates', async page => {
  api.mockResolvedValue(page)
  await expect(captureArchiveOwnerAfterDrain('chat', undefined, undefined, {} as never)).rejects.toBe(unresolved)
})

it('does not queue permission refusal as drain', async () => {
  api.mockRejectedValue(new Error('403: permission_denied'))
  await expect(captureArchiveOwnerAfterDrain('chat', undefined, undefined, {} as never)).rejects.toThrow('permission_denied')
  expect(api).toHaveBeenCalledOnce()
  expect(enqueue).not.toHaveBeenCalled()
})

it('holds a projection failure carried in the native aggregate errors envelope', async () => {
  api.mockResolvedValueOnce({ sessions: [], total: 0, offset: 0, errors: [{ error: '503: sessions projection unavailable' }] })
    .mockResolvedValueOnce({ sessions: [row], total: 1, offset: 0 })
  await expect(captureArchiveOwnerAfterDrain('chat', undefined, undefined, {} as never)).resolves.toEqual(row)
  expect(enqueue).toHaveBeenCalledOnce()
  expect(api.mock.calls[0][1].path).toContain('archive_owner_lookup=1')
})

it('deduplicates pin backfill across cursor pages', async () => {
  const first = [...Array.from({ length: 300 }, (_, n) => ({ ...row, id: `other-${n}` })), row]
  api.mockResolvedValueOnce({ sessions: first, total: 301, offset: 0 })
    .mockResolvedValueOnce({ sessions: [row], total: 301, offset: 300 })
  await expect(captureArchiveOwnerAfterDrain('chat', undefined, undefined, {} as never)).resolves.toEqual(row)
  expect(api).toHaveBeenCalledTimes(2)
})

it('preserves an already captured unlisted primary invocation', async () => {
  capture.mockReturnValueOnce({ connection_id: 'hub' })
  await expect(captureArchiveOwnerAfterDrain('chat', undefined, undefined, {} as never)).resolves.toEqual({ connection_id: 'hub' })
  expect(api).not.toHaveBeenCalled()
})

it('scans every captured gateway and refuses a same-id sibling on a second gateway', async () => {
  primary.connections = ['hub', 'other']
  api.mockResolvedValue({ sessions: [row], total: 1, offset: 0 })
  await expect(captureArchiveOwnerAfterDrain('chat', undefined, undefined, {} as never)).rejects.toBe(unresolved)
  expect(api.mock.calls.map(([scope]) => scope.connectionId)).toEqual(['hub', 'other'])
})

it('does not prove uniqueness when another registered gateway is unavailable', async () => {
  primary.connections = ['hub', 'other']
  api.mockResolvedValueOnce({ sessions: [row], total: 1, offset: 0 })
    .mockRejectedValueOnce(new Error('ECONNREFUSED'))
  await expect(captureArchiveOwnerAfterDrain('chat', undefined, undefined, {} as never)).rejects.toThrow('ECONNREFUSED')
})

it('refuses a topology edit during ownership recovery', async () => {
  api.mockImplementationOnce(async () => {
    primary.connections = ['hub', 'other']
    return { sessions: [row], total: 1, offset: 0 }
  })
  await expect(captureArchiveOwnerAfterDrain('chat', undefined, undefined, {} as never)).rejects.toBe(unresolved)
})
