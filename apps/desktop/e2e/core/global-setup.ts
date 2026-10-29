import { assertShmLargeEnough } from '../shm-preflight'

/** Fail the whole core lane in seconds on a host that cannot paint Electron. */
export default function globalSetup(): void {
  assertShmLargeEnough()
}
