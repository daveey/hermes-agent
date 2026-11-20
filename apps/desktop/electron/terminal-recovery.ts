// Selected-account recovery: the one command an account-recovery PTY may run.
//
// The renderer names a provider and a stored credential row; it never supplies
// command text. Everything that reaches an argv or a remote command line is
// checked against the patterns here first, so a request that does not match
// starts nothing (no shell, no fallback to another host).

/** Providers whose sign-in must run in an interactive CLI on the gateway. */
const RECOVERY_PROVIDERS = new Set(['anthropic'])

// A pool row id / profile name as Hermes writes them. The leading character
// rules out an option-looking token (`-x`), and the alphabet rules out quoting,
// whitespace and shell syntax.
const CREDENTIAL_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/
const PROFILE_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/

export interface RecoveryRequest {
  credentialId: string
  provider: string
}

/** The recovery a start payload names, or null when any field is absent,
 *  mistyped or outside the allowed alphabet. */
export function requestedRecovery(payload: unknown): null | RecoveryRequest {
  const record = payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {}
  const { credentialId, provider } = record

  if (typeof provider !== 'string' || !RECOVERY_PROVIDERS.has(provider)) {
    return null
  }

  if (typeof credentialId !== 'string' || !CREDENTIAL_ID.test(credentialId)) {
    return null
  }

  return { credentialId, provider }
}

/** `hermes` arguments for the recovery, or null when the profile name is not a
 *  plain profile name. */
export function recoveryCliArgs(request: RecoveryRequest, profile?: string): null | string[] {
  const named = (profile || '').trim()

  if (named && !PROFILE_NAME.test(named)) {
    return null
  }

  return [...(named ? ['--profile', named] : []), 'auth', 'reauth', request.provider, request.credentialId]
}

const shq = (value: string) => `'${value.replace(/'/g, `'\\''`)}'`

/** The single remote command an SSH recovery runs: the gateway's own Hermes
 *  launcher with the checked arguments, replacing the login shell so the PTY
 *  ends when the sign-in does. Null without a launcher path. */
export function remoteRecoveryCommand(
  runtime: { hermesHome?: null | string; hermesPath?: null | string },
  args: readonly string[]
): null | string {
  const hermesPath = String(runtime.hermesPath || '').trim()

  if (!hermesPath || /[\0\r\n]/.test(hermesPath)) {
    return null
  }

  const hermesHome = String(runtime.hermesHome || '').trim()

  if (/[\0\r\n]/.test(hermesHome)) {
    return null
  }

  const env = hermesHome ? `HERMES_HOME=${shq(hermesHome)} ` : ''

  return `${env}exec ${shq(hermesPath)} ${args.map(shq).join(' ')}`
}
