import { closeSync, fsyncSync, openSync } from 'node:fs'
import type { FileHandle } from 'node:fs/promises'

// The only places ui-tui asks the kernel to put bytes on stable storage (lint-enforced).
// A flush waits for the whole device: on a shared CI disk one fsync behind other jobs'
// writeback has taken over 5 s. Suites that assert queue/renderer logic swap this module
// for no-ops (vitest.setup.ts); durableFlush.test.ts owns the ordering contract.

export function flushFileSync(fd: number): void {
  fsyncSync(fd)
}

// Makes a rename or unlink inside `dir` survive a crash.
export function flushDirectorySync(dir: string): void {
  if (process.platform === 'win32') {
    return
  }

  const fd = openSync(dir, 'r')

  try {
    fsyncSync(fd)
  } finally {
    closeSync(fd)
  }
}

export async function flushFileHandle(handle: FileHandle): Promise<void> {
  await handle.sync()
}
