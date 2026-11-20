import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useEffect, useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'
import * as areas from '@/sdk/areas'
import { stubMenuDomApis } from '@/test/jsdom'

import { group, split } from '../model'
import {
  $collapsedTreeSides,
  $dismissedPanes,
  $hiddenTreePanes,
  $layoutTree,
  $narrowViewport,
  activateTreePane
} from '../store'

import { LayoutTreeRoot } from '.'

const disposers: (() => void)[] = []
beforeEach(() => {
  stubMenuDomApis()
  $hiddenTreePanes.set(new Set())
  $dismissedPanes.set(new Set())
  $collapsedTreeSides.set(new Set())
  $narrowViewport.set(false)

  for (const id of ['sessions', 'workspace', 'page', 'session-tile:other', 'terminal']) {
    disposers.push(registry.register({ id, area: 'panes', title: id, render: () => <div>{id}</div> }))
  }

  $layoutTree.set(
    split('row', [
      group(['sessions'], { id: 'sidebar' }),
      group(['workspace', 'page', 'session-tile:other'], { id: 'main' }),
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

it('exports the main-zone area', () => {
  expect(areas).toHaveProperty('MAIN_PANE_TOP_AREA', 'workspace.mainTop')
})

it('places ordered contributions inside only the workspace zone, below its titlebar', () => {
  disposers.push(
    registry.register({ id: 'later', area: 'workspace.mainTop', order: 20, render: () => <button>Later</button> })
  )
  disposers.push(
    registry.register({ id: 'first', area: 'workspace.mainTop', order: 10, render: () => <button>First</button> })
  )
  const { container } = render(<LayoutTreeRoot titlebar />)
  const strip = container.querySelector('[data-main-pane-top]')!
  expect(strip).not.toBeNull()
  expect(strip.closest('[data-tree-group]')?.getAttribute('data-tree-group')).toBe('main')
  expect(strip.textContent).toBe('FirstLater')
  expect(strip.previousElementSibling?.hasAttribute('data-panel-header')).toBe(true)
  expect(container.querySelector('[data-tree-group="sidebar"] [data-main-pane-top]')).toBeNull()
  expect(container.querySelector('[data-tree-group="side"] [data-main-pane-top]')).toBeNull()
  expect(strip.className).toContain('shrink-0')
})

it('retains contributor state across session and page switches', () => {
  let mounts = 0

  function Strip() {
    const [count, setCount] = useState(0)
    useEffect(() => {
      mounts++
    }, [])

    return <button onClick={() => setCount(count + 1)}>State {count}</button>
  }

  disposers.push(registry.register({ id: 'persistent', area: 'workspace.mainTop', render: () => <Strip /> }))
  render(<LayoutTreeRoot />)
  fireEvent.click(screen.getByText('State 0'))
  const button = screen.getByText('State 1')
  act(() => activateTreePane('main', 'session-tile:other'))
  expect(screen.getByText('State 1')).toBe(button)
  act(() => activateTreePane('main', 'page'))
  expect(screen.getByText('State 1')).toBe(button)
  act(() => activateTreePane('main', 'workspace'))
  expect(mounts).toBe(1)
})

it('adds no box when empty and no padding, borders or height for null-rendering contributions', () => {
  const { container } = render(<LayoutTreeRoot />)
  expect(container.querySelector('[data-main-pane-top]')).toBeNull()
  act(() => {
    disposers.push(registry.register({ id: 'null', area: 'workspace.mainTop', render: () => null }))
  })
  const strip = container.querySelector<HTMLElement>('[data-main-pane-top]')!
  expect(strip).not.toBeNull()
  expect(strip.children).toHaveLength(0)
  expect(strip.style.height).toBe('')
  expect(strip.className).not.toMatch(/\b(?:p-|border|h-)/)
})
