import { expect, test } from 'vitest'
import { temporaryMemoryMiB } from '../.github/scripts/workspace-check-temp-memory.mjs'
import { memoryBudgetMiB } from '../.github/scripts/workspace-check-admission.mjs'

test.runIf(process.platform === 'linux')('tmpfs capacity is reserved in addition to the normal runner reserve', () => {
  const reserve = temporaryMemoryMiB('/fixture', () => ({ type: 0x01021994, bsize: 4096, blocks: 262144 }))
  expect(reserve).toBe(1024)
  expect(memoryBudgetMiB({ constrained: 8 * 2 ** 30, total: 64 * 2 ** 30, temporaryMiB: reserve })).toBe(6144)
})

test('disk-backed temporary storage leaves the memory budget unchanged', () => {
  const reserve = temporaryMemoryMiB('/fixture', () => ({ type: 0x794c7630, bsize: 4096, blocks: 262144 }))
  expect(reserve).toBe(0)
  expect(memoryBudgetMiB({ constrained: 8 * 2 ** 30, total: 64 * 2 ** 30, temporaryMiB: reserve })).toBe(7168)
})
