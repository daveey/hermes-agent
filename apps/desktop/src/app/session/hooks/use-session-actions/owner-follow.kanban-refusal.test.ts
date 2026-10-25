import { describe, expect, it } from 'vitest'

import { isKanbanWorkerResumeRefusal } from './owner-follow'

describe('isKanbanWorkerResumeRefusal', () => {
  it('matches the gateway 4132 refusal only', () => {
    expect(isKanbanWorkerResumeRefusal(Object.assign(new Error('worker'), { code: 4132 }))).toBe(true)
    expect(isKanbanWorkerResumeRefusal(Object.assign(new Error('nf'), { code: 4007 }))).toBe(false)
    expect(isKanbanWorkerResumeRefusal(new Error('x'))).toBe(false)
    expect(isKanbanWorkerResumeRefusal(null)).toBe(false)
  })
})
