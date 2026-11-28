import { createHash } from 'node:crypto'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

import { afterEach, expect, it, vi } from 'vitest'

import { companionIdentity, readK01Companion } from './k01-companion.test-helpers'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllEnvs()
})

it('canonical default loads the complete pinned actual companion offline', () => {
  vi.stubEnv('HERMES_K01_COMPANION', undefined)
  const source = readK01Companion()

  expect(Buffer.byteLength(source)).toBe(companionIdentity.byteLength)
  expect(createHash('sha256').update(source).digest('hex')).toBe(companionIdentity.sha256)
})

it('explicit replay reads exactly the supplied source rather than substituting the pin', () => {
  const path = fileURLToPath(import.meta.url)
  vi.stubEnv('HERMES_K01_COMPANION', path)
  expect(readK01Companion()).toBe(fs.readFileSync(path, 'utf8'))
  expect(readK01Companion()).not.toBe(readK01CompanionFixture())
})

function readK01CompanionFixture() {
  vi.stubEnv('HERMES_K01_COMPANION', undefined)

  return readK01Companion()
}

it.each(['', '/missing-k01-replay/plugin.js'])('invalid explicit input %j fails without fallback', input => {
  vi.stubEnv('HERMES_K01_COMPANION', input)
  const read = vi.spyOn(fs, 'readFileSync')
  expect(() => readK01Companion()).toThrow()
  expect(read).toHaveBeenCalledTimes(1)
})

it.each(['length', 'digest'])('corrupt pinned %s fails closed before integration collection', kind => {
  const original = Buffer.from(readK01CompanionFixture())
  const corrupted = kind === 'length' ? original.subarray(1) : Buffer.from(original)
  corrupted[0] ^= 1
  vi.spyOn(fs, 'readFileSync').mockReturnValueOnce(corrupted)
  expect(() => readK01Companion()).toThrow('K01 companion fixture differs from')
})
