import { expect, it } from 'vitest'

import { archiveQueueLocksFixture } from './web-locks'

it('supports the two-argument request used by composer drains', async () => {
  const { locks, held } = archiveQueueLocksFixture()
  const request = locks as unknown as LockManager
  expect(await request.request('composer', lock => lock?.name)).toBe('composer')
  expect(held.size).toBe(0)
})

it('serializes blocking requests while nonblocking peers see a held lock', async () => {
  const { locks, held } = archiveQueueLocksFixture()
  const request = locks as unknown as LockManager
  let release!: () => void
  const order: string[] = []

  const first = request.request('composer', async () => {
    order.push('first')
    await new Promise<void>(resolve => {
      release = resolve
    })
  })

  const second = request.request('composer', () => {
    order.push('second')
  })
  expect(await request.request('composer', { ifAvailable: true }, lock => lock)).toBeNull()
  expect(order).toEqual(['first'])
  release()
  await Promise.all([first, second])
  expect(order).toEqual(['first', 'second'])
  expect(held.size).toBe(0)
})
