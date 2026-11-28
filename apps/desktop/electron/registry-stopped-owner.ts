import { lockMatchesManagedUpdateScope, readLockfile, shq } from './remote-lifecycle'

interface ExpectedOwner {
  ownershipId: string
  pid: number
  spawnNonce: string
  startedAt: string
  creationTime: string
  profile: string
  hermesPath: string
  hermesHome: string
}

/** Read-only recovery for an ambiguous retirement commit. Never reuse the
 * live terminator: absence of an HTTP response grants no signal authority.
 * System Python remains usable after the managed runtime has been deleted.
 */
export async function proveRegistryOwnerStopped(
  ssh: { exec: (command: string) => Promise<string> },
  expected: ExpectedOwner
): Promise<boolean> {
  if (!Number.isSafeInteger(expected.pid) || expected.pid <= 0 || !expected.creationTime?.startsWith('linux:')) {
    return false
  }

  const lock = await readLockfile(ssh, expected.ownershipId)

  if (!lockMatchesManagedUpdateScope(lock, expected)) {
    return false
  }

  // Unlike shell `kill -0 || DEAD`, EPERM and all other unknown errors must
  // fail closed. Signal zero only probes; no process can be terminated here.
  const script = `import os,sys
if not sys.platform.startswith('linux'):
 print('UNKNOWN')
else:
 try:
  os.kill(${expected.pid}, 0)
  print('LIVE')
 except ProcessLookupError:
  print('STOPPED')
 except OSError:
  print('UNKNOWN')
`

  const verdict = String(await ssh.exec(`python3 -c ${shq(script)}`)).trim()

  if (verdict !== 'STOPPED') {
    return false
  }

  // A replacement ownership record during the process probe must not be
  // cleaned up as the old resident, even when its old PID is dead.
  return lockMatchesManagedUpdateScope(await readLockfile(ssh, expected.ownershipId), expected)
}
