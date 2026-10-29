import * as fs from 'node:fs'

/**
 * Refuse to start an Electron E2E suite on a Linux host whose /dev/shm is too
 * small for Chromium.
 *
 * Docker gives a container 64 MiB of /dev/shm unless it is started with
 * --shm-size. Under that, the Electron renderer never paints: the backend comes
 * up, but the window stays blank (`body text:` empty), so every spec times out
 * at its own limit. On CI 40785530488 that was 9 x 180 s of identical timeouts
 * before the 30 min job limit cancelled the lane, and no failure text was ever
 * printed. This check fails the run in about a second instead, naming the cause.
 */
export const MIN_SHM_BYTES = 512 * 1024 * 1024

export interface ShmProbe {
  platform: NodeJS.Platform
  /** Free bytes on /dev/shm, or null when it cannot be measured. */
  shmBytes: () => null | number
}

export function shmProblem(probe: ShmProbe, min = MIN_SHM_BYTES): null | string {
  if (probe.platform !== 'linux') {
    return null
  }

  const bytes = probe.shmBytes()

  if (bytes === null || bytes >= min) {
    return null
  }

  const mib = (n: number) => `${Math.round(n / (1024 * 1024))} MiB`

  return (
    `/dev/shm has ${mib(bytes)} total; Electron needs at least ${mib(min)}. With less, the renderer stays ` +
    `blank and every spec times out. Start the runner container with --shm-size (2g on the hermes CI ` +
    `boxes), or set HERMES_E2E_ALLOW_SMALL_SHM=1 to run anyway.`
  )
}

export function realShmBytes(): null | number {
  try {
    const s = fs.statfsSync('/dev/shm')

    return s.blocks * s.bsize
  } catch {
    return null
  }
}

export function assertShmLargeEnough(probe: ShmProbe = { platform: process.platform, shmBytes: realShmBytes }): void {
  if (process.env.HERMES_E2E_ALLOW_SMALL_SHM === '1') {
    return
  }

  const problem = shmProblem(probe)

  if (problem) {
    throw new Error(problem)
  }
}
