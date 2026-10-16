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
})
