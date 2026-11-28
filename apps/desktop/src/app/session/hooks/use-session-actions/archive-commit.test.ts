import { beforeEach, expect, it, vi } from 'vitest'

const { setSessionArchived, notifyError } = vi.hoisted(() => ({
  setSessionArchived: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('@/hermes', () => ({ setSessionArchived }))
vi.mock('@/store/notifications', () => ({ notifyError }))
vi.mock('./archive-queue-lifetime', () => ({
  archiveQueueLifetime: () => ({
    run: (run: () => Promise<unknown>) => run(),
    enqueue: vi.fn(),
    sleep: async () => {},
    dispose: vi.fn()
  })
}))

import { commitArchive } from './archive-commit'

const copy = { archiveFailed: 'Archive failed' } as never
const STALLED = new Error(
  '503: owner_unavailable: control_timeout: the gateway that owns this profile did not answer in time; nothing was changed, retry'
)

function hiddenRow() {
  return { end: vi.fn(), hid: true, ids: ['chat'], restore: vi.fn() }
}

beforeEach(() => {
  setSessionArchived.mockReset()
  notifyError.mockReset()
})

it('restores the row on failure and offers a Retry bound to the same owner and id', async () => {
  const owner = { connectionId: 'hermes-hub', profile: 'default' }
  const first = hiddenRow()
  const again = hiddenRow()
  const rehide = vi.fn(() => again)
  const onArchived = vi.fn()
  setSessionArchived.mockRejectedValueOnce(STALLED).mockResolvedValueOnce({ ok: true })

  expect(
    await commitArchive({
      copy,
      retryLabel: 'Retry',
      hidden: first,
      onArchived,
      owner,
      rehide,
      storedSessionId: 'chat'
    })
  ).toBe(false)
  expect(first.restore).toHaveBeenCalledOnce()
  expect(first.end).not.toHaveBeenCalled()
  expect(onArchived).not.toHaveBeenCalled()
  const [error, title, options] = notifyError.mock.calls[0]
  expect([error, title, options.id]).toEqual([STALLED, 'Archive failed', 'archive-failed:chat'])
  expect(options.action.label).toBe('Retry')

  options.action.onClick()
  await vi.waitFor(() => expect(onArchived).toHaveBeenCalledOnce())
  expect(rehide).toHaveBeenCalledOnce()
  expect(again.end).toHaveBeenCalledOnce()
  expect(again.restore).not.toHaveBeenCalled()
  // Both sends target the exact same owner object and session id.
  expect(setSessionArchived.mock.calls).toEqual([
    ['chat', true, owner],
    ['chat', true, owner]
  ])
  expect(setSessionArchived.mock.calls[1][2]).toBe(owner)
})

it('a failed Retry restores the row again and keeps offering Retry', async () => {
  const owner = { profile: 'work' }
  const again = hiddenRow()
  setSessionArchived.mockRejectedValue(STALLED)
  await commitArchive({
    copy,
    retryLabel: 'Retry',
    hidden: hiddenRow(),
    onArchived: vi.fn(),
    owner,
    rehide: () => again,
    storedSessionId: 'chat'
  })
  notifyError.mock.calls[0][2].action.onClick()
  await vi.waitFor(() => expect(again.restore).toHaveBeenCalledOnce())
  expect(notifyError).toHaveBeenCalledTimes(2)
  expect(notifyError.mock.calls[1][2].action.label).toBe('Retry')
})

it('a definitive refusal restores the row with no Retry', async () => {
  const hidden = hiddenRow()
  setSessionArchived.mockRejectedValueOnce(new Error('409: revision_conflict'))
  await commitArchive({
    copy,
    retryLabel: 'Retry',
    hidden,
    onArchived: vi.fn(),
    owner: 'work',
    rehide: hiddenRow,
    storedSessionId: 'chat'
  })
  expect(hidden.restore).toHaveBeenCalledOnce()
  expect(notifyError.mock.calls[0][2].action).toBeUndefined()
})

it('holds a draining PATCH then commits once, keeping the owner and suppressing errors', async () => {
  const owner = { connectionId: 'hermes-hub', profile: 'work' }
  const hidden = hiddenRow()
  const onArchived = vi.fn()
  setSessionArchived
    .mockRejectedValueOnce(new Error('503: {"detail":"owner_unavailable: draining: Exclusive maintenance refused"}'))
    .mockRejectedValueOnce(new Error('socket hang up'))
    .mockResolvedValueOnce({ ok: true })
  expect(
    await commitArchive({
      copy,
      retryLabel: 'Retry',
      hidden,
      onArchived,
      owner,
      rehide: hiddenRow,
      storedSessionId: 'drain'
    })
  ).toBe(true)
  expect(onArchived).toHaveBeenCalledOnce()
  expect(hidden.restore).not.toHaveBeenCalled()
  expect(hidden.end).toHaveBeenCalledOnce()
  expect(notifyError).not.toHaveBeenCalled()
  expect(setSessionArchived.mock.calls).toEqual(Array.from({ length: 3 }, () => ['drain', true, owner]))
})

it('success ends the hide without a toast', async () => {
  const hidden = hiddenRow()
  const onArchived = vi.fn()
  setSessionArchived.mockResolvedValueOnce({ ok: true })
  expect(
    await commitArchive({
      copy,
      retryLabel: 'Retry',
      hidden,
      onArchived,
      owner: 'work',
      rehide: hiddenRow,
      storedSessionId: 'chat'
    })
  ).toBe(true)
  expect(onArchived).toHaveBeenCalledOnce()
  expect(hidden.end).toHaveBeenCalledOnce()
  expect(hidden.restore).not.toHaveBeenCalled()
  expect(notifyError).not.toHaveBeenCalled()
})
