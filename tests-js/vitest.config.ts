import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    environment: 'node',
    include: ['**/*.test.ts', '**/*.test.mjs'],
    // Several files shell out to real git/python/npm. vitest's 5s default
    // times them out on a loaded shared runner; match the electron project.
    testTimeout: 30_000,
  },
})
