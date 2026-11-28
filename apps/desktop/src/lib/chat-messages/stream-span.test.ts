import { describe, expect, it } from 'vitest'

import { appendAssistantTextPart, closingStreamRun, textPart } from '@/lib/chat-messages'

const span = (start: number, end: number, segment = 0) => ({ turnId: 'turn-1', segment, start, end })

describe('appendAssistantTextPart with stamped offsets', () => {
  it('appends contiguous chunks and extends the claim', () => {
    const parts = appendAssistantTextPart(
      appendAssistantTextPart([], 'Hello ', undefined, span(0, 6)),
      'world',
      undefined,
      span(6, 11)
    )

    expect(parts).toEqual([{ ...textPart('Hello world'), streamSpan: span(0, 11) }])
  })

  it('drops a chunk the part already holds and keeps only the unseen tail of an overlap', () => {
    const held = appendAssistantTextPart([], 'Hello wor', undefined, span(0, 9))

    expect(appendAssistantTextPart(held, 'lo wo', undefined, span(3, 8))).toBe(held)
    expect(appendAssistantTextPart(held, 'world', undefined, span(6, 11))).toEqual([
      { ...textPart('Hello world'), streamSpan: span(0, 11) }
    ])
  })

  it('claims only the stamped tail of unclaimed text, and a gap restarts the claim', () => {
    const journal = [textPart('A2 ')]
    const stamped = appendAssistantTextPart(journal, 'NEXT ', undefined, span(8, 13))

    expect(closingStreamRun({ id: 'a', role: 'assistant', parts: stamped })).toEqual({
      span: span(8, 13),
      text: 'NEXT '
    })
    expect(appendAssistantTextPart(stamped, 'LATE', undefined, span(20, 24)).at(-1)).toMatchObject({
      text: 'A2 NEXT LATE',
      streamSpan: span(20, 24)
    })
  })

  it('an unstamped chunk after a claimed run withdraws the claim', () => {
    const parts = appendAssistantTextPart(appendAssistantTextPart([], 'stamped', undefined, span(0, 7)), ' plain')

    expect(parts).toEqual([textPart('stamped plain')])
    expect(closingStreamRun({ id: 'a', role: 'assistant', parts })).toBeNull()
  })

  it('counts UTF-16 units, so an astral character overlaps exactly', () => {
    const held = appendAssistantTextPart([], 'A 😀', undefined, span(0, 4))

    expect(appendAssistantTextPart(held, '😀 B', undefined, span(2, 6)).at(-1)).toMatchObject({
      text: 'A 😀 B',
      streamSpan: span(0, 6)
    })
  })
})

describe('closingStreamRun', () => {
  it('reads only the prose after the last tool boundary', () => {
    const tool = { type: 'tool-call', toolCallId: 't', toolName: 'terminal', args: {} } as const

    expect(
      closingStreamRun({ id: 'a', role: 'assistant', parts: [{ ...textPart('before'), streamSpan: span(0, 6) }, tool] })
    ).toBeNull()
  })

  it('refuses a claim longer than the text it is on', () => {
    expect(
      closingStreamRun({ id: 'a', role: 'assistant', parts: [{ ...textPart('abc'), streamSpan: span(0, 9) }] })
    ).toBeNull()
  })
})
