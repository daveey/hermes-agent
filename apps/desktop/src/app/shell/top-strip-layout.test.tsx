// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react'
import { useEffect } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'

import { group } from '@/components/pane-shell/tree/model'
import { declareDefaultTree } from '@/components/pane-shell/tree/store'
import { registry } from '@/contrib/registry'
import { I18nProvider } from '@/i18n'
import { stubMenuDomApis } from '@/test/jsdom'

import { ShellWorkspace } from './top-strip'

const disposers: (() => void)[] = []
afterEach(() => {
  cleanup()
  disposers.splice(0).forEach(dispose => dispose())
  vi.unstubAllGlobals()
})

it('keeps the real pane tree below shell chrome without remounting its bodies when the strip changes', () => {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
  )
  stubMenuDomApis()
  const mounted = vi.fn()

  function Body() {
    useEffect(mounted, [])

    return <input aria-label="Unsaved text" defaultValue="keep me" />
  }

  disposers.push(
    registry.register({
      id: 'workspace',
      area: 'panes',
      title: 'Workspace',
      data: { placement: 'main', uncloseable: true },
      render: () => <Body />
    })
  )
  declareDefaultTree(group(['workspace'], { id: 'top-strip-main', active: 'workspace' }))

  const { container } = render(
    <MemoryRouter>
      <I18nProvider configClient={null} initialLocale="en">
        <ShellWorkspace />
      </I18nProvider>
    </MemoryRouter>
  )

  const body = screen.getByLabelText('Unsaved text')
  act(() =>
    disposers.push(
      registry.register({
        id: 'strip',
        area: 'shell.topStrip',
        render: () => <button>Global strip</button>
      })
    )
  )
  const strip = container.querySelector('[data-shell-top-strip]')!
  const pane = container.querySelector('[data-tree-group]')!
  expect(strip.contains(pane)).toBe(false)
  expect(strip.nextElementSibling?.contains(pane)).toBe(true)
  expect(screen.getByLabelText('Unsaved text')).toBe(body)
  act(() => disposers.pop()!())
  expect(container.querySelector('[data-shell-top-strip]')).toBeNull()
  expect(screen.getByLabelText('Unsaved text')).toBe(body)
  expect(mounted).toHaveBeenCalledTimes(1)
})
