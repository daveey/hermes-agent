import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'
import * as areas from '@/sdk/areas'
import { stubMenuDomApis } from '@/test/jsdom'

import { group, split } from '../model'
import { $collapsedTreeSides, $dismissedPanes, $hiddenTreePanes, $layoutTree, $narrowViewport } from '../store'

import { LayoutTreeRoot } from '.'

const disposers: (() => void)[] = []
beforeEach(() => {
  stubMenuDomApis()
  $hiddenTreePanes.set(new Set())
  $dismissedPanes.set(new Set())
  $collapsedTreeSides.set(new Set())
  $narrowViewport.set(false)

  for (const id of ['sessions', 'bots', 'workspace', 'terminal']) {
    disposers.push(registry.register({ id, area: 'panes', title: id, render: () => <div>{id}</div> }))
  }

  $layoutTree.set(
    split('row', [
      group(['sessions', 'bots'], { id: 'sidebar' }),
      group(['workspace'], { id: 'main' }),
      group(['terminal'], { id: 'side' })
    ])
  )
})
afterEach(() => {
  cleanup()
  disposers.splice(0).forEach(dispose => dispose())
  $layoutTree.set(null)
  vi.restoreAllMocks()
})

it('exports the sessions tab-strip area', () => {
  expect(areas).toHaveProperty('SESSIONS_TAB_STRIP_AREA', 'sessions.tabStrip')
})

it('mounts ordered contributions after the tabs of only the Sessions zone strip', () => {
  disposers.push(
    registry.register({ id: 'later', area: 'sessions.tabStrip', order: 20, render: () => <button>Later</button> })
  )
  disposers.push(
    registry.register({ id: 'first', area: 'sessions.tabStrip', order: 10, render: () => <button>First</button> })
  )
  const { container } = render(<LayoutTreeRoot />)
  const slots = container.querySelectorAll('[data-sessions-tab-strip]')
  expect(slots).toHaveLength(1)
  const slot = slots[0]
  expect(slot.closest('[data-tree-group]')?.getAttribute('data-tree-group')).toBe('sidebar')
  expect(slot.textContent).toBe('FirstLater')
  // Beside the tabs, outside the scrolling tab list.
  const strip = slot.closest('[data-zone-tabstrip]')!
  expect(strip).not.toBeNull()
  expect(slot.closest('[role="tablist"]')).toBeNull()
  expect(strip.querySelector('[role="tablist"]')?.textContent).toContain('bots')
  expect(slot.className).toContain('max-w-[65%]')
})

it('keeps clicks in the slot from starting a pane drag', () => {
  const clicked = vi.fn()
  disposers.push(registry.register({ id: 'btn', area: 'sessions.tabStrip', render: () => <button onClick={clicked}>Go</button> }))
  const { getByText } = render(<LayoutTreeRoot />)
  const pointer = new Event('pointerdown', { bubbles: true })
  const stop = vi.spyOn(pointer, 'stopPropagation')
  getByText('Go').dispatchEvent(pointer)
  expect(stop).toHaveBeenCalled()
  fireEvent.click(getByText('Go'))
  expect(clicked).toHaveBeenCalledOnce()
})

it('adds no box while the area is empty', () => {
  const { container } = render(<LayoutTreeRoot />)
  expect(container.querySelector('[data-sessions-tab-strip]')).toBeNull()
  act(() => {
    disposers.push(registry.register({ id: 'late', area: 'sessions.tabStrip', render: () => <span>Late</span> }))
  })
  expect(container.querySelector('[data-sessions-tab-strip]')?.textContent).toBe('Late')
})
