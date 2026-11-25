import { describe, expect, it } from 'vitest'

import { TITLEBAR_TOOL_MAX_SPAN, titlebarToolCells, titlebarToolSpan, titlebarToolsWidthCss } from './titlebar'

describe('titlebar tool span', () => {
  it('defaults to one cell and clamps to whole cells in 1..max', () => {
    expect(titlebarToolSpan({})).toBe(1)
    expect(titlebarToolSpan({ span: 5 })).toBe(5)
    expect(titlebarToolSpan({ span: 2.7 })).toBe(2)
    expect(titlebarToolSpan({ span: 0 })).toBe(1)
    expect(titlebarToolSpan({ span: -3 })).toBe(1)
    expect(titlebarToolSpan({ span: Number.NaN })).toBe(1)
    expect(titlebarToolSpan({ span: 99 })).toBe(TITLEBAR_TOOL_MAX_SPAN)
    expect(titlebarToolSpan({ span: '4' as unknown as number })).toBe(4)
  })

  it('a row reserves the sum of its spans, so a wide chip is not drawn over the header text', () => {
    expect(titlebarToolCells([])).toBe(0)
    expect(titlebarToolCells([{}, {}])).toBe(2)
    expect(titlebarToolCells([{}, { span: 5 }])).toBe(6)
    expect(titlebarToolsWidthCss(titlebarToolCells([{}, { span: 5 }]))).toBe('calc(6 * var(--titlebar-control-size))')
  })
})
