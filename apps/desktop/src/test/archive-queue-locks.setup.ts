import { afterEach, beforeEach, vi } from 'vitest'

import { archiveQueueLocksFixture } from './web-locks'

// Opt in only archive consumers. Global Web Locks also activates unrelated
// prepared-submission custody paths whose tests deliberately model no Web Locks.
beforeEach(() => {
  vi.stubGlobal('navigator', { locks: archiveQueueLocksFixture().locks })
})
afterEach(() => vi.unstubAllGlobals())
