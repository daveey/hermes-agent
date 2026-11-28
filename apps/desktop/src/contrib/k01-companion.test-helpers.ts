import { createHash } from 'node:crypto'
import fs from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const companionIdentity = {
  repository: 'https://github.com/daveey/hermes-mods',
  commit: 'ff969f65b26ee67d700f3dc604adee78a34b1060',
  path: 'desktop-plugins/session-tasks/plugin.js',
  sha256: '6ad5b99582d61a48154818361c4471c820e05c8c0f8d9b916ef012cc8c927f61',
  byteLength: 88346
} as const

// The default is a byte-exact real companion, not a reduced implementation.
// Explicit replay inputs remain caller-owned; invalid paths must not fall back.
export function readK01Companion(input = process.env.HERMES_K01_COMPANION): string {
  if (input !== undefined) {
    return fs.readFileSync(input, 'utf8')
  }
  const bytes = fs.readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), 'fixtures/k01/session-tasks.plugin.js.txt')
  )

  if (
    bytes.length !== companionIdentity.byteLength ||
    createHash('sha256').update(bytes).digest('hex') !== companionIdentity.sha256
  ) {
    throw new Error(`K01 companion fixture differs from ${companionIdentity.commit}:${companionIdentity.path}`)
  }

  return bytes.toString('utf8')
}
