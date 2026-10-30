import { statfsSync } from 'node:fs'

/**
 * tmpfs pages count against the same cgroup as the check processes. Reserve
 * the whole bounded mount, not its current use, before starting those writers.
 * @param {string} dir
 * @param {(dir: string) => {type: number, bsize: number, blocks: number}} read
 */
export function temporaryMemoryMiB(dir, read = statfsSync) {
  if (process.platform !== 'linux') return 0
  const fs = read(dir)
  return fs.type === 0x01021994 ? Math.ceil(fs.bsize * fs.blocks / 2 ** 20) : 0
}
