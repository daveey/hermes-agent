import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { StatusbarControls, statusbarToggleLabel } from '@/app/shell/statusbar-controls'
import { ContribRender } from '@/contrib/react/boundary'
import { registry } from '@/contrib/registry'
import { $statusbarHiddenIds, resetStatusbarLayout } from '@/store/statusbar-prefs'
import { stubMenuDomApis, stubResizeObserver } from '@/test/jsdom'

import { useStatusbarContributions } from './panes'

// Registrations are global (module-level registry); each test captures its
// own disposer and this undoes it, so tests don't bleed into each other.
let dispose: (() => void) | undefined

afterEach(() => {
  cleanup()
  dispose?.()
  dispose = undefined
  resetStatusbarLayout()
})

// A stateful contribution — a chip that opens a dialog-like panel and holds
// that open/closed state itself, the same shape a real plugin uses.
function StatefulChip() {
  const [open, setOpen] = useState(false)

  return (
    <div>
      <button onClick={() => setOpen(true)} type="button">
        open
      </button>
      {open && <div data-testid="panel">panel open</div>}
    </div>
  )
}

// Hosts the contributed items and carries UNRELATED state of its own, so
// clicking "bump" re-renders the host (and re-invokes useStatusbarContributions)
// without touching the registry — exactly what a streaming message or a
// session-timer tick does to the real statusbar.
function Host() {
  const [, setTick] = useState(0)
  const items = useStatusbarContributions('right')

  return (
    <div>
      <button onClick={() => setTick(t => t + 1)} type="button">
        bump
      </button>
      {items.map(item =>
        // Mirror the real consumer exactly (statusbar-controls.tsx): `item.render`
        // is passed as a PROP and mounted via `createElement(render)` inside
        // ContribRender — not invoked inline. Calling it directly here would
        // dodge the bug entirely (the wrapper's own identity never gets used
        // as a component type, so no remount could ever be observed).
        item.render ? <ContribRender key={item.id} render={item.render} /> : null
      )}
    </div>
  )
}

describe('useStatusbarContributions', () => {
  it('lists every real contribution and filters custom renders before mounting them', async () => {
    stubMenuDomApis()
    stubResizeObserver()
    const draw = vi.fn(() => <span>runner custom content</span>)
    dispose = registry.registerMany([
      { area: 'statusBar.right', id: 'runners:ci-chip', render: draw },
      {
        area: 'statusBar.right',
        id: 'declared',
        data: { id: 'existing-id', toggleLabel: 'My custom control', label: 'custom data', lockedVisible: true }
      }
    ])
    $statusbarHiddenIds.set(['runners:ci-chip'])

    function Bar() {
      const items = useStatusbarContributions('right')

      return (
        <>
          <div data-testid="expected-labels">{JSON.stringify(items.map(statusbarToggleLabel))}</div>
          <StatusbarControls items={items} />
        </>
      )
    }

    render(
      <MemoryRouter>
        <Bar />
      </MemoryRouter>
    )
    expect(draw).not.toHaveBeenCalled()
    const bar = screen.getByRole('contentinfo')
    fireEvent.pointerDown(bar, { button: 2, pointerType: 'mouse' })
    fireEvent.contextMenu(bar, { button: 2 })
    const rows = await screen.findAllByRole('menuitemcheckbox')
    expect(rows.map(row => row.textContent)).toEqual(JSON.parse(screen.getByTestId('expected-labels').textContent!))
    const custom = screen.getByRole('menuitemcheckbox', { name: 'Runners Ci Chip' })
    expect(custom.getAttribute('data-disabled')).toBeNull()
    fireEvent.click(custom)
    expect(screen.getByText('runner custom content')).toBeTruthy()
    expect($statusbarHiddenIds.get()).not.toContain('runners:ci-chip')
    fireEvent.click(custom)
    expect(screen.queryByText('runner custom content')).toBeNull()
    fireEvent.click(screen.getByRole('menuitemcheckbox', { name: 'My custom control' }))
    expect(screen.queryByText('custom data')).toBeNull()
    expect($statusbarHiddenIds.get()).toEqual(['runners:ci-chip', 'existing-id'])
    expect(JSON.parse(window.localStorage.getItem('hermes.desktop.statusbarHidden.v3')!)).toEqual(
      $statusbarHiddenIds.get()
    )
  })

  it('keeps a render-contribution mounted (state intact) across an unrelated host re-render', () => {
    dispose = registry.register({ area: 'statusBar.right', id: 'stateful-chip', render: () => <StatefulChip /> })

    render(<Host />)

    // Open the panel — this is the state that must survive.
    fireEvent.click(screen.getByRole('button', { name: 'open' }))
    expect(screen.getByTestId('panel')).toBeTruthy()

    // Re-render the host for a reason that has nothing to do with the
    // registry (a streaming tick, a session switch elsewhere in the tree).
    fireEvent.click(screen.getByRole('button', { name: 'bump' }))

    // #91603: before the fix, useStatusbarContributions handed back a new
    // `render` closure identity every call, so ContribRender's
    // createElement(render) read it as a different component type and
    // remounted StatefulChip — dropping `open` back to false.
    expect(screen.getByTestId('panel')).toBeTruthy()
  })
})
