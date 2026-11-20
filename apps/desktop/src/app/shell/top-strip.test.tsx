// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'
import * as areas from '@/sdk/areas'

import { ShellWorkspace } from './top-strip'

vi.mock('@/components/pane-shell/tree/renderer', () => ({
  LayoutTreeRoot: ({ titlebar }: { titlebar: boolean }) => (
    <div data-testid="workspace" data-titlebar={String(titlebar)}>
      Tabs and panes
    </div>
  )
}))
vi.mock('./use-touch-titlebar', () => ({ useTitlebarHeight: () => 34 }))

const disposers: (() => void)[] = []
const resizeCallbacks = new Set<() => void>()
beforeEach(() => {
  // jsdom has no layout; model the strip's natural block height from its content.
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    return { height: this.querySelector('button') ? 24 : 0 } as DOMRect
  })
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(private callback: () => void) {}
      observe() {
        resizeCallbacks.add(this.callback)
      }
      disconnect() {
        resizeCallbacks.delete(this.callback)
      }
    }
  )
})
afterEach(() => {
  cleanup()
  disposers.splice(0).forEach(dispose => dispose())
  expect(resizeCallbacks.size).toBe(0)
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('keeps old geometry for a registered component returning null, including internal state transitions', () => {
  let setVisible!: (visible: boolean) => void

  function Strip() {
    const [visible, update] = useState(false)
    setVisible = update

    return visible ? <button>Working</button> : null
  }

  disposers.push(registry.register({ id: 'conditional', area: 'shell.topStrip', render: () => <Strip /> }))
  const { container } = render(<ShellWorkspace />)
  const workspace = screen.getByTestId('workspace')

  const assertGeometry = (visible: boolean) => {
    expect(container.querySelector('[data-shell-titlebar-band]') !== null).toBe(visible)
    expect(workspace.dataset.titlebar).toBe(String(!visible))
    expect(screen.getByTestId('workspace')).toBe(workspace)
  }

  assertGeometry(false)
  act(() => setVisible(true))
  act(() => resizeCallbacks.forEach(callback => callback()))
  assertGeometry(true)
  act(() => setVisible(false))
  act(() => resizeCallbacks.forEach(callback => callback()))
  assertGeometry(false)
})

it('exports the shell-wide area alongside the statusbar areas', () => {
  expect(areas).toHaveProperty('TOP_STRIP_AREA', 'shell.topStrip')
})

it('reserves no strip or separate titlebar space without renderable contributions', () => {
  disposers.push(registry.register({ id: 'data-only', area: 'shell.topStrip', data: {} }))
  disposers.push(registry.register({ id: 'disabled', area: 'shell.topStrip', enabled: false, render: () => 'hidden' }))
  const { container } = render(<ShellWorkspace />)
  expect(container.children).toHaveLength(1)
  expect(screen.getByTestId('workspace').dataset.titlebar).toBe('true')
})

it('places ordered full-width contributions below the titlebar and above all tabs, then removes the space on unload', () => {
  const { container } = render(<ShellWorkspace />)
  act(() => {
    disposers.push(
      registry.register({ id: 'later', area: 'shell.topStrip', order: 20, render: () => <button>Later</button> })
    )
    disposers.push(
      registry.register({ id: 'first', area: 'shell.topStrip', order: 10, render: () => <button>First</button> })
    )
  })
  const strip = container.querySelector('[data-shell-top-strip]')!
  expect(strip.textContent).toBe('FirstLater')
  expect(strip.className).toContain('w-full')
  expect(strip.className).toContain('shrink-0')
  expect(strip.previousElementSibling?.hasAttribute('data-shell-titlebar-band')).toBe(true)
  expect((strip.previousElementSibling as HTMLElement).style.height).toBe('34px')
  expect(strip.nextElementSibling).toBe(screen.getByTestId('workspace'))
  expect(screen.getByTestId('workspace').dataset.titlebar).toBe('false')
  act(() => disposers.splice(0).forEach(dispose => dispose()))
  expect(container.children).toHaveLength(1)
  expect(screen.getByTestId('workspace').dataset.titlebar).toBe('true')
})
