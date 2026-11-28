import type { ReactNode } from 'react'

import { Tip } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

/** The shared card-category palette. Every numbered card square (sidebar Background counts, plugin strips and
 *  panels) picks one of these, so a colour means the same thing everywhere. `outline` / `dashed` are uncoloured
 *  categories (triage/other, sessions with no cards). */
export const CARD_COUNT_CATEGORIES = [
  'red',
  'pink',
  'orange',
  'green',
  'amber',
  'deploy',
  'blue',
  'grey',
  'violet',
  'unknown',
  'outline',
  'dashed'
] as const

export type CardCountCategory = (typeof CARD_COUNT_CATEGORIES)[number]

const BORDERS: Record<CardCountCategory, string> = {
  red: 'border-red-500',
  pink: 'border-pink-500',
  orange: 'border-orange-500',
  green: 'border-emerald-500',
  amber: 'border-amber-500',
  deploy: 'border-[#a371f7]',
  blue: 'border-sky-400 dark:border-sky-500',
  grey: 'border-zinc-400 dark:border-zinc-500',
  violet: 'border-violet-500',
  unknown: 'border-zinc-400 dark:border-zinc-500',
  outline: 'border-(--ui-text-tertiary)',
  dashed: 'border-dashed border-(--ui-text-tertiary)'
}

export interface CardCountItem {
  category: CardCountCategory
  count: ReactNode
  /** Words for this category; joined into the group's tooltip / aria-label as `<label> <count>`. */
  label?: string
}

/** One numbered card square: the count sits inside an outline in the category's colour. */
export function CardCountSquare({ category, count, className }: CardCountItem & { className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex h-4 min-w-4 shrink-0 items-center justify-center rounded-[2px] border px-[2px] text-[0.625rem] leading-none tabular-nums text-(--ui-text-secondary)',
        BORDERS[category] ?? BORDERS.unknown,
        className
      )}
      data-card-colour={category}
    >
      {count}
    </span>
  )
}

/** A row of numbered card squares with one tooltip naming every category. Empty input renders nothing. */
export function CardCountSquares({
  items,
  className,
  wrap = false,
  testId = 'card-count-squares'
}: {
  items: readonly CardCountItem[]
  className?: string
  wrap?: boolean
  testId?: string
}) {
  if (!items.length) {
    return null
  }

  const label = items
    .map(({ label: words, category, count }) => `${words ?? category} ${typeof count === 'object' ? '' : count}`.trim())
    .join(' · ')

  return (
    <Tip label={label} side="top">
      <span
        aria-label={label}
        className={cn(
          'pointer-events-auto inline-flex items-center gap-[2px] align-middle',
          wrap && 'flex-wrap justify-end',
          className
        )}
        data-testid={testId}
        role="img"
      >
        {items.map(item => (
          <CardCountSquare category={item.category} count={item.count} key={item.category} />
        ))}
      </span>
    </Tip>
  )
}
