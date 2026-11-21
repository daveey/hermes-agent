import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { fetchProjectSessions } from '@/store/projects'

import type { SidebarProjectTree } from './projects/workspace-groups'
import { useEnteredProjectSessions } from './use-entered-project-sessions'

vi.mock('@/store/projects', () => ({ fetchProjectSessions: vi.fn() }))
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const node = (id: string): SidebarProjectTree => ({
  id,
  label: 'app',
  path: '/code/app',
  repos: [],
  sessionCount: 0,
  sessionIds: []
})

function deferred() {
  let resolve!: (value: SidebarProjectTree | null) => void
  let reject!: (error: Error) => void

  const promise = new Promise<SidebarProjectTree | null>((yes, no) => {
    resolve = yes
    reject = no
  })

  return { promise, resolve, reject }
}

it('keeps singleton fetch arguments unchanged', async () => {
  vi.mocked(fetchProjectSessions).mockResolvedValue(node('one'))
  const { result } = renderHook(() => useEnteredProjectSessions('one', true, [], 'scope'))
  await waitFor(() => expect(result.current.loading).toBe(false))
  expect(fetchProjectSessions).toHaveBeenCalledWith('one')
})
it('hydrates members independently without supersession; retries only the failed member', async () => {
  const first = deferred()
  const second = deferred()
  vi.mocked(fetchProjectSessions)
    .mockReturnValueOnce(first.promise)
    .mockReturnValueOnce(second.promise)
    .mockResolvedValueOnce(node('two'))

  const { result } = renderHook(() => ({
    one: useEnteredProjectSessions('one', true, [], 'scope', {
      supersedable: false
    }),
    two: useEnteredProjectSessions('two', true, [], 'scope', {
      supersedable: false
    })
  }))

  expect(result.current.one.loading).toBe(true)
  expect(result.current.two.loading).toBe(true)
  await act(async () => second.reject(new Error('offline')))
  expect(result.current.two.failed).toBe(true)
  expect(result.current.two.loading).toBe(false)
  expect(result.current.one.loading).toBe(true)
  await act(async () => first.resolve(node('one')))
  expect(result.current.one.project?.id).toBe('one')
  act(() => result.current.two.retry())
  await waitFor(() => expect(result.current.two.project?.id).toBe('two'))
  expect(fetchProjectSessions).toHaveBeenNthCalledWith(1, 'one', {
    supersedable: false
  })
  expect(fetchProjectSessions).toHaveBeenNthCalledWith(2, 'two', {
    supersedable: false
  })
  expect(fetchProjectSessions).toHaveBeenCalledTimes(3)
})
it('ignores late success and failure after switching member scope', async () => {
  const old = deferred()
  const next = deferred()
  vi.mocked(fetchProjectSessions).mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise)

  const { result, rerender } = renderHook(
    ({ id }) => useEnteredProjectSessions(id, true, [], id, { supersedable: false }),
    { initialProps: { id: 'old' } }
  )

  rerender({ id: 'next' })
  await act(async () => next.resolve(node('next')))
  await act(async () => old.resolve(node('old')))
  expect(result.current.project?.id).toBe('next')
  expect(result.current.failed).toBe(false)
})
