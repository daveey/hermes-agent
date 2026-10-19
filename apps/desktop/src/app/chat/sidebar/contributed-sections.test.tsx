import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SIDEBAR_SECTIONS_AREA } from '@/app/routes'
import { registry } from '@/contrib/registry'

import { SidebarContributedSections, SidebarSection, SidebarSectionRow } from './contributed-sections'

// [daveey] `sidebar.sections`: plugin lists mounted between Pinned and Projects.

let dispose: (() => void) | undefined

afterEach(() => {
  dispose?.()
  dispose = undefined
  cleanup()
})

function Demo({ onSelect }: { onSelect: (id: string) => void }) {
  const [open, setOpen] = useState(true)
  const [visible, setVisible] = useState(1)
  const rows = ['alpha', 'beta', 'gamma']

  return (
    <SidebarSection
      count={rows.length}
      hiddenCount={rows.length - visible}
      label="Blocked"
      onLoadMore={() => setVisible(n => n + 10)}
      onToggle={() => setOpen(!open)}
      open={open}
    >
      {rows.slice(0, visible).map(id => (
        <SidebarSectionRow key={id} label={id} meta="2h" onSelect={() => onSelect(id)} />
      ))}
    </SidebarSection>
  )
}

describe('SidebarContributedSections', () => {
  it('renders nothing without contributions', () => {
    const { container } = render(<SidebarContributedSections />)

    expect(container.innerHTML).toBe('')
  })

  it('mounts a contributed section; rows click, load more and collapse', () => {
    const onSelect = vi.fn()
    dispose = registry.register({ area: SIDEBAR_SECTIONS_AREA, id: 'demo', render: () => <Demo onSelect={onSelect} /> })

    render(<SidebarContributedSections />)

    expect(screen.getByText('Blocked')).toBeTruthy()
    expect(screen.getByText('3')).toBeTruthy()
    expect(screen.getByText('2h')).toBeTruthy()

    fireEvent.click(screen.getByText('alpha'))
    expect(onSelect).toHaveBeenCalledWith('alpha')

    expect(screen.queryByText('beta')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /2/ }))
    expect(screen.getByText('gamma')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { expanded: true }))
    expect(screen.queryByText('alpha')).toBeNull()
  })

  it('drops a section when its contribution is disposed', () => {
    dispose = registry.register({ area: SIDEBAR_SECTIONS_AREA, id: 'demo', render: () => <Demo onSelect={vi.fn()} /> })
    render(<SidebarContributedSections />)
    expect(screen.getByText('Blocked')).toBeTruthy()

    act(() => {
      dispose?.()
      dispose = undefined
    })
    expect(screen.queryByText('Blocked')).toBeNull()
  })

  it('renders hover actions beside meta without selecting the row, and bolds strong labels', () => {
    const onSelect = vi.fn()
    const onArchive = vi.fn()

    const { container } = render(
      <SidebarSection label="Inbox" onToggle={() => {}} open>
        <SidebarSectionRow
          actions={
            <button
              onClick={event => {
                event.stopPropagation()
                onArchive()
              }}
              type="button"
            >
              Archive
            </button>
          }
          label="Idle box"
          meta="12m"
          onSelect={onSelect}
          strong
        />
        <SidebarSectionRow label="Plain" meta="1d" onSelect={() => {}} />
      </SidebarSection>
    )

    const slot = container.querySelector('[data-slot="sidebar-section-row-actions"]')
    expect(slot).not.toBeNull()
    expect(slot?.className).toContain('group-hover/contrib-row:flex')
    expect(screen.getByText('12m').className).toContain('group-hover/contrib-row:hidden')
    // A row without actions keeps its meta visible on hover.
    expect(screen.getByText('1d').className).not.toContain('hidden')
    expect(container.querySelectorAll('[data-slot="sidebar-section-row-actions"]')).toHaveLength(1)

    fireEvent.click(screen.getByText('Archive'))
    expect(onArchive).toHaveBeenCalledTimes(1)
    expect(onSelect).not.toHaveBeenCalled()

    expect(screen.getByText('Idle box').className).toContain('font-semibold')
    expect(screen.getByText('Plain').className).not.toContain('font-semibold')
  })
})
