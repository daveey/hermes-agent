import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { isFileDrag, useFileDrop } from './attach-drop'

const fileTransfer = (files: File[]) => ({ types: ['Files'], files, dropEffect: 'none' })
const cardTransfer = () => ({ types: ['text/plain'], files: [], dropEffect: 'none', getData: () => 't_1' })

function Zone({ onFiles, enabled = true }: { onFiles: (files: File[]) => void; enabled?: boolean }) {
  const drop = useFileDrop(onFiles, enabled)

  return (
    <div data-over={drop.over ? 'yes' : 'no'} data-testid="zone" {...drop.handlers}>
      <span data-testid="child">child</span>
    </div>
  )
}

afterEach(cleanup)

describe('isFileDrag', () => {
  it('accepts OS file drags and rejects card moves', () => {
    expect(isFileDrag({ types: ['Files'] } as unknown as DataTransfer)).toBe(true)
    expect(isFileDrag({ types: ['text/plain'] } as unknown as DataTransfer)).toBe(false)
    expect(isFileDrag(null)).toBe(false)
  })
})

describe('useFileDrop', () => {
  it('highlights while files hover (child crossings included) and hands over dropped files', () => {
    const onFiles = vi.fn()
    render(<Zone onFiles={onFiles} />)
    const zone = screen.getByTestId('zone')
    const file = new File(['x'], 'a.txt')

    fireEvent.dragEnter(zone, { dataTransfer: fileTransfer([file]) })
    fireEvent.dragEnter(screen.getByTestId('child'), { dataTransfer: fileTransfer([file]) })
    fireEvent.dragLeave(zone, { dataTransfer: fileTransfer([file]) })
    expect(zone.dataset.over).toBe('yes')

    act(() => {
      fireEvent.drop(zone, { dataTransfer: fileTransfer([file]) })
    })
    expect(zone.dataset.over).toBe('no')
    expect(onFiles).toHaveBeenCalledWith([file])
  })

  it('ignores card moves so lane drops still reach the column', () => {
    const onFiles = vi.fn()
    const laneDrop = vi.fn()
    render(
      <div onDrop={laneDrop}>
        <Zone onFiles={onFiles} />
      </div>
    )
    const zone = screen.getByTestId('zone')

    fireEvent.dragEnter(zone, { dataTransfer: cardTransfer() })
    expect(zone.dataset.over).toBe('no')
    fireEvent.drop(zone, { dataTransfer: cardTransfer() })
    expect(onFiles).not.toHaveBeenCalled()
    expect(laneDrop).toHaveBeenCalledOnce()
  })

  it('stops file drops from bubbling to the lane', () => {
    const laneDrop = vi.fn()
    render(
      <div onDrop={laneDrop}>
        <Zone onFiles={vi.fn()} />
      </div>
    )
    fireEvent.drop(screen.getByTestId('zone'), { dataTransfer: fileTransfer([new File(['x'], 'a.txt')]) })
    expect(laneDrop).not.toHaveBeenCalled()
  })

  it('does nothing while disabled', () => {
    const onFiles = vi.fn()
    render(<Zone enabled={false} onFiles={onFiles} />)
    fireEvent.drop(screen.getByTestId('zone'), { dataTransfer: fileTransfer([new File(['x'], 'a.txt')]) })
    expect(onFiles).not.toHaveBeenCalled()
  })
})
