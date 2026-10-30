import { vi } from 'vitest'

// A physical flush costs whatever the shared disk is doing, not what the code under test does:
// on CI one fsync behind the concurrent checks' writeback has outlasted the 5 s test budget.
// Suites still write, rename and read the real journal; only the flush is a no-op here.
// src/__tests__/durableFlush.test.ts asserts flush ordering and the real module's syscalls.
vi.mock('./src/lib/durableFlush.js', () => ({
  flushDirectorySync: vi.fn(),
  flushFileHandle: vi.fn(async () => {}),
  flushFileSync: vi.fn()
}))
