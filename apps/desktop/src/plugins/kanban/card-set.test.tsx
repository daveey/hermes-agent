import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { $cardSet, CardSetBoard, installCardSetHook, SHOW_CARDS_HOOK } from './card-set'
import { KANBAN_LOCALES } from './i18n'

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

let locales = () => {}
beforeEach(() => { locales = registerPluginLocales('kanban', KANBAN_LOCALES) })
afterEach(() => { cleanup(); client.clear(); $cardSet.set(null); locales() })

it('opens exactly the owner-qualified board/card set, snapshots input and clears the filter', async () => {
  const target: Record<string, unknown> = {}
  const navigate = vi.fn()
  const dispose = installCardSetHook({ navigate }, target)
  const hook = target[SHOW_CARDS_HOOK] as (request: unknown) => boolean
  const request = { connectionId: 'owner-a', cards: [{ board: 'one', id: 'same' }, { board: 'two', id: 'other' }] }
  expect(hook(request)).toBe(true)
  request.cards[0].id = 'injected'
  request.connectionId = 'wrong'
  expect(navigate).toHaveBeenCalledWith('/kanban')

  const rest = vi.fn(async (path: string, opts: unknown) => {
    expect(opts).toEqual({ connectionId: 'owner-a', profile: null })
    const board = new URL(path, 'https://example.com').searchParams.get('board')

    return { columns: [{ name: 'ready', tasks: ['same', 'other', 'injected'].map(id => ({ id, title: `${board}/${id}`, status: 'ready' })) }] }
  })

  render(<QueryClientProvider client={client}><CardSetBoard rest={rest as never} /></QueryClientProvider>)
  await screen.findByText('one/same')
  await screen.findByText('two/other')
  expect(screen.queryByText('one/other')).toBeNull()
  expect(screen.queryByText('two/same')).toBeNull()
  expect(screen.queryByText(/injected/)).toBeNull()
  expect(rest).toHaveBeenCalledTimes(2)
  fireEvent.click(screen.getByRole('button', { name: 'Clear card filter' }))
  expect($cardSet.get()).toBeNull()
  dispose()
  expect(target[SHOW_CARDS_HOOK]).toBeUndefined()
})

it.each([null, {}, { cards: [], connectionId: '' }, { cards: [{ board: '', id: 'x' }], connectionId: 'a' }, { cards: [{ board: 'a', id: '' }], connectionId: 'a' }])('rejects incomplete routes without changing selection: %j', request => {
  const target: Record<string, unknown> = {}
  const navigate = vi.fn()
  const dispose = installCardSetHook({ navigate }, target)
  expect((target[SHOW_CARDS_HOOK] as (request: unknown) => boolean)(request)).toBe(false)
  expect($cardSet.get()).toBeNull()
  expect(navigate).not.toHaveBeenCalled()
  dispose()
})

it('retains the last owner request on invalid input and does not remove a replacement hook', () => {
  const target: Record<string, unknown> = {}
  const dispose = installCardSetHook({ navigate: vi.fn() }, target)
  const hook = target[SHOW_CARDS_HOOK] as (request: unknown) => boolean
  act(() => { hook({ connectionId: 'a', cards: [] }) })
  expect(hook({ cards: [] })).toBe(false)
  expect($cardSet.get()).toEqual({ connectionId: 'a', cards: [] })
  const replacement = () => true
  target[SHOW_CARDS_HOOK] = replacement
  dispose()
  expect(target[SHOW_CARDS_HOOK]).toBe(replacement)
})
