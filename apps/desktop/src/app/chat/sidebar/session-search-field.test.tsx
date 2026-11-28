// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { requestSessionSearchFocus } from '@/store/layout'

import { SessionSearchField } from './session-search-field'

function Harness({ onKeyDown = () => {} }: { onKeyDown?: () => void }) {
  const [query, setQuery] = useState('')

  return (
    <div onKeyDown={onKeyDown}>
      <SessionSearchField onQueryChange={setQuery} query={query} />
      <output data-testid="query">{JSON.stringify(query)}</output>
    </div>
  )
}

const search = () => screen.queryByRole('textbox', { name: 'Search sessions' })

const reveal = async () => {
  act(() => requestSessionSearchFocus())
  await waitFor(() => expect(search()).not.toBeNull())

  return screen.getByRole('textbox', { name: 'Search sessions' }) as HTMLInputElement
}

afterEach(cleanup)

describe('SessionSearchField dismissal boundaries', () => {
  it('resets whitespace on blur, not only disclosure state', async () => {
    render(<Harness />)
    const input = await reveal()
    fireEvent.change(input, { target: { value: '   ' } })
    act(() => input.blur())
    expect(search()).toBeNull()
    expect(screen.getByTestId('query').textContent).toBe('""')
    expect((await reveal()).value).toBe('')
  })

  it('hides on Escape even without a blur and stops ancestor keyboard actions', async () => {
    const ancestor = vi.fn()
    render(<Harness onKeyDown={ancestor} />)
    const input = await reveal()
    // Keep disclosure dismissal distinct from the empty-blur path. The
    // input is deliberately not blurred by this event or a global key handler.
    fireEvent.change(input, { target: { value: 'retained filter' } })
    expect(document.activeElement).toBe(input)
    expect(fireEvent.keyDown(input, { key: 'Escape' })).toBe(false)
    expect(ancestor).not.toHaveBeenCalled()
    expect(search()).toBeNull()
    expect(screen.getByTestId('query').textContent).toBe('""')
    expect((await reveal()).value).toBe('')
  })

  it('does not swallow ordinary keyboard events or hide active filters on blur', async () => {
    const ancestor = vi.fn()
    render(<Harness onKeyDown={ancestor} />)
    const input = await reveal()
    fireEvent.change(input, { target: { value: 'active' } })
    expect(fireEvent.keyDown(input, { key: 'Enter' })).toBe(true)
    expect(ancestor).toHaveBeenCalledOnce()
    act(() => input.blur())
    expect(search()).toBe(input)
    expect(input.value).toBe('active')
  })
})
