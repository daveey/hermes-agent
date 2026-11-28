import { resolve } from 'node:path'

import { defineConfig } from 'vitest/config'
export default defineConfig({
  root: resolve(import.meta.dirname, '../..'),
  resolve: { alias: { '@': resolve(import.meta.dirname, '..'), '@hermes/plugin-sdk': resolve(import.meta.dirname, '../sdk/index.ts'), '@hermes/shared': resolve(import.meta.dirname, '../../../shared/src') } },
  esbuild: { jsx: 'automatic' },
  test: { environment: 'jsdom', include: ['src/test/session-state-v2-golden.test.tsx'], testTimeout: 15000 }
})
