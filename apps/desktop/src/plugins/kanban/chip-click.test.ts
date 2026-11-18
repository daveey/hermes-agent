import { describe, expect, it } from 'vitest'

import { chipClickOpensBoard, KANBAN_CHIP_EVENT } from './chip-click'

describe('status-bar task pill click', () => {
  it('opens the board when nobody takes the click over', () => {
    const target = new EventTarget()
    let seen = 0
    target.addEventListener(KANBAN_CHIP_EVENT, () => { seen += 1 })
    expect(chipClickOpensBoard(target)).toBe(true)
    expect(seen).toBe(1)
  })

  it('does not open the board when a listener prevents the default', () => {
    const target = new EventTarget()
    target.addEventListener(KANBAN_CHIP_EVENT, event => event.preventDefault())
    expect(chipClickOpensBoard(target)).toBe(false)
  })

  it('opens the board again once that listener is gone', () => {
    const target = new EventTarget()
    const take = (event: Event) => event.preventDefault()
    target.addEventListener(KANBAN_CHIP_EVENT, take)
    target.removeEventListener(KANBAN_CHIP_EVENT, take)
    expect(chipClickOpensBoard(target)).toBe(true)
  })
})
