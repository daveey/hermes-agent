/**
 * `@baby bear`: an `@` popover backed by a multi-word source (session titles)
 * stays open across a space while its rows still match, and closes once they
 * stop. Without a multi-word row the space ends the token as before.
 */
import type { Unstable_TriggerAdapter, Unstable_TriggerItem } from '@assistant-ui/core'
import { act, renderHook } from '@testing-library/react'
import { createRef } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { composerPlainText, renderComposerContents, RICH_INPUT_SLOT } from '../rich-editor'
import { continueAtTrigger } from '../text-utils'

import { useComposerTrigger } from './use-composer-trigger'

function placeCaretAtEnd(editor: HTMLDivElement) {
  const range = document.createRange()
  range.selectNodeContents(editor)
  range.collapse(false)
  const selection = window.getSelection()!
  selection.removeAllRanges()
  selection.addRange(range)
}

const row = (title: string, multiWord: boolean): Unstable_TriggerItem => ({
  id: title,
  type: 'simple',
  label: title,
  metadata: {
    icon: 'session',
    display: title,
    meta: '',
    rawText: `@session:${title}`,
    insertId: '',
    multiWord: multiWord ? '1' : ''
  }
})

/** Every query word must prefix a word of the title — the session source's rule. */
function sourceAdapter(titles: string[], multiWord = true): Unstable_TriggerAdapter {
  return {
    categories: () => [],
    categoryItems: () => [],
    search: (query: string) => {
      const q = query.toLowerCase().split(/\s+/).filter(Boolean)

      return titles
        .filter(t =>
          q.every(w =>
            t
              .toLowerCase()
              .split(/\s+/)
              .some(tw => tw.startsWith(w))
          )
        )
        .map(t => row(t, multiWord))
    }
  }
}

function setup(adapter: Unstable_TriggerAdapter) {
  const editor = document.createElement('div')
  editor.dataset.slot = RICH_INPUT_SLOT
  editor.contentEditable = 'true'
  document.body.append(editor)

  const editorRef = createRef<HTMLDivElement>() as { current: HTMLDivElement | null }
  editorRef.current = editor
  const draftRef = { current: '' }

  const hook = renderHook(() =>
    useComposerTrigger({
      at: { adapter, loading: false },
      draftRef,
      editorRef,
      requestMainFocus: vi.fn(),
      setComposerText: vi.fn(),
      slash: { adapter: null, loading: false }
    })
  )

  /** Type `text` one character at a time, refreshing like keyup does. */
  const type = (text: string) => {
    for (const ch of text) {
      renderComposerContents(editor, composerPlainText(editor) + ch)
      placeCaretAtEnd(editor)
      act(() => hook.result.current.refreshTrigger())
    }
  }

  return { editor, hook, type }
}

describe('multi-word @ queries', () => {
  it('keeps the popover open across a space while session titles match', () => {
    const { hook, type } = setup(sourceAdapter(['baby bear hub restart', 'baby monitor']))

    type('@baby be')

    expect(hook.result.current.trigger).toMatchObject({ kind: '@', query: 'baby be', tokenLength: 8 })
    expect(hook.result.current.triggerItems.map(i => i.label)).toEqual(['baby bear hub restart'])
  })

  it('closes once the widened query stops matching', () => {
    const { hook, type } = setup(sourceAdapter(['baby bear hub restart']))

    // The first unmatched character closes it — no "No matches" popover
    // lingering over the prose that follows.
    type('@baby p')

    expect(hook.result.current.trigger).toBeNull()
    expect(hook.result.current.triggerItems).toHaveLength(0)

    type('lease')
    expect(hook.result.current.trigger).toBeNull()
  })

  it('picks the match and replaces the whole multi-word token', () => {
    const { editor, hook, type } = setup(sourceAdapter(['baby bear hub restart']))

    type('see @baby bea')
    act(() => hook.result.current.replaceTriggerWithChip(hook.result.current.triggerItems[0]!))

    expect(composerPlainText(editor)).not.toContain('bea ')
    expect(composerPlainText(editor).startsWith('see ')).toBe(true)
    expect(hook.result.current.trigger).toBeNull()
  })

  it('control: a space still ends an @ token whose rows are not multi-word', () => {
    const { hook, type } = setup(sourceAdapter(['baby bear hub restart'], false))

    type('@baby b')

    expect(hook.result.current.trigger).toBeNull()
  })

  it('control: Escape then a space does not reopen the widened popover', () => {
    const { hook, type } = setup(sourceAdapter(['baby bear hub restart']))

    type('@baby')
    act(() => hook.result.current.closeTrigger())
    type(' b')

    expect(hook.result.current.trigger).toBeNull()
  })
})

describe('continueAtTrigger', () => {
  const prev = { kind: '@' as const, query: 'baby', tokenLength: 5, value: 'baby' }

  it('widens the same token across a space', () => {
    expect(continueAtTrigger('hi @baby ', prev, true)).toEqual({
      kind: '@',
      query: 'baby ',
      tokenLength: 6,
      value: 'baby '
    })
  })

  it('refuses without a multi-word row, a scope, a newline, or a different token', () => {
    expect(continueAtTrigger('@baby ', prev, false)).toBeNull()
    expect(continueAtTrigger('@file:a b', { ...prev, query: 'file:a', scope: 'file' }, true)).toBeNull()
    expect(continueAtTrigger('@baby\nb', prev, true)).toBeNull()
    expect(continueAtTrigger('@other x', prev, true)).toBeNull()
    expect(continueAtTrigger('@baby', prev, true)).toBeNull()
  })

  it('caps the widened token at six words', () => {
    const long = { ...prev, query: 'a b c d e f' }

    expect(continueAtTrigger('@a b c d e f', { ...prev, query: 'a b c d e ' }, true)).not.toBeNull()
    expect(continueAtTrigger('@a b c d e f g', long, true)).toBeNull()
  })

  it('only continues an @ trigger', () => {
    expect(continueAtTrigger('@baby ', { ...prev, kind: '/' }, true)).toBeNull()
    expect(continueAtTrigger('@baby ', null, true)).toBeNull()
  })
})
