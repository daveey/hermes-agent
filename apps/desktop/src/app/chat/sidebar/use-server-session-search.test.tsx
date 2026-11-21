import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { searchSessions } from '@/hermes'

import { SidebarSearchFailure } from './search-states'
import { useServerSessionSearch } from './use-server-session-search'

vi.mock('./section-states', () => ({ SidebarSessionSkeletons: () => null }))

vi.mock('@/hermes', () => ({ searchSessions: vi.fn() }))
vi.mock('@/store/profile', () => ({ ALL_PROFILES: '*', normalizeProfileKey: (key: string) => key }))

async function searchSettles() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(200)
  })
}

describe('server session search failures', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.mocked(searchSessions).mockReset()
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('exposes failures and retries the same query and profile successfully', async () => {
    vi.mocked(searchSessions).mockRejectedValueOnce(new Error('Search timed out'))
    vi.mocked(searchSessions).mockResolvedValueOnce({ results: [] })
    const { result } = renderHook(() => useServerSessionSearch('Face', 'work'))
    await searchSettles()
    expect(result.current.searchFailed).toBe(true)
    expect(result.current.searchPending).toBe(false)
    expect(result.current.serverMatches).toEqual([])
    act(() => result.current.retrySearch())
    expect(result.current.searchFailed).toBe(false)
    expect(result.current.searchPending).toBe(true)
    await searchSettles()
    expect(searchSessions).toHaveBeenNthCalledWith(2, 'Face', 'work')
    expect(result.current.searchFailed).toBe(false)
    expect(result.current.searchPending).toBe(false)
  })

  it('shows a retry alert alongside loaded matches and clears it after retry', async () => {
    vi.mocked(searchSessions).mockRejectedValueOnce(new Error('offline'))
    vi.mocked(searchSessions).mockResolvedValueOnce({ results: [] })

    function Harness() {
      const { searchFailed, retrySearch } = useServerSessionSearch('Face', 'default')

      return (
        <>
          <span>Loaded local match</span>
          <SidebarSearchFailure label="Search failed, retry" retry={retrySearch} visible={searchFailed} />
        </>
      )
    }

    render(<Harness />)
    await searchSettles()
    expect(screen.getByRole('alert').textContent).toBe('Search failed, retry')
    expect(screen.getByText('Loaded local match')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Search failed, retry' }))
    await searchSettles()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(searchSessions).toHaveBeenCalledTimes(2)
  })

  it('clears failure when the query is cleared', async () => {
    vi.mocked(searchSessions).mockRejectedValue(new Error('offline'))

    const { result, rerender } = renderHook(({ query }) => useServerSessionSearch(query, 'default'), {
      initialProps: { query: 'Face' }
    })

    await searchSettles()
    expect(result.current.searchFailed).toBe(true)
    rerender({ query: '' })
    expect(result.current.searchFailed).toBe(false)
    expect(result.current.searchPending).toBe(false)
  })

  it('ignores a stale rejection after the profile changes', async () => {
    let rejectOld!: (error: Error) => void
    vi.mocked(searchSessions).mockReturnValueOnce(
      new Promise((_, reject) => {
        rejectOld = reject
      })
    )
    vi.mocked(searchSessions).mockResolvedValueOnce({ results: [] })

    const { result, rerender } = renderHook(({ profile }) => useServerSessionSearch('Face', profile), {
      initialProps: { profile: 'old' }
    })

    await searchSettles()
    rerender({ profile: 'new' })
    await searchSettles()
    await act(async () => {
      rejectOld(new Error('old request failed'))
    })
    expect(result.current.searchFailed).toBe(false)
    expect(result.current.searchPending).toBe(false)
  })
})
