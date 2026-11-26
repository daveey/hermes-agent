import { cleanup, render } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import * as sdk from '@/sdk'

import { CardCountSquare, CardCountSquares } from './card-count-squares'

afterEach(cleanup)

it('puts each count inside its category square, in input order, with one tooltip label', () => {
  const { container } = render(
    <CardCountSquares
      items={[
        { category: 'red', count: 2, label: 'Needs you' },
        { category: 'violet', count: 5, label: 'Held at a gate' },
        { category: 'dashed', count: 7, label: 'No cards' }
      ]}
    />
  )

  const group = container.querySelector('[data-testid="card-count-squares"]')!
  expect(group.getAttribute('aria-label')).toBe('Needs you 2 · Held at a gate 5 · No cards 7')
  const squares = [...group.querySelectorAll('[data-card-colour]')]
  expect(squares.map(s => [s.getAttribute('data-card-colour'), s.textContent])).toEqual([
    ['red', '2'],
    ['violet', '5'],
    ['dashed', '7']
  ])
  expect(squares[0].className).toContain('border-red-500')
  expect(squares[2].className).toContain('border-dashed')
})

it('renders nothing for no categories, and a lone square carries no tooltip wrapper', () => {
  const { container } = render(<CardCountSquares items={[]} />)
  expect(container.innerHTML).toBe('')

  const lone = render(<CardCountSquare category="blue" count={3} />)
  expect(lone.container.querySelector('[data-card-colour="blue"]')?.textContent).toBe('3')
  expect(lone.container.querySelector('[role="img"]')).toBeNull()
})

it('is exported to plugins through the SDK', () => {
  expect(sdk.CardCountSquares).toBe(CardCountSquares)
  expect(sdk.CardCountSquare).toBe(CardCountSquare)
  expect(sdk.CARD_COUNT_CATEGORIES).toContain('outline')
})
