import { descriptorCustody } from '@/contrib/diagnostic-primitives'

interface HostGraph {
  sessions: object
  composer: object
  diagnostics: object
  state: Record<string, object>
}

/** Called once immediately after SDK construction, never against published globals. */
export function sessionTasksHostCustody(host: HostGraph): () => boolean {
  const checks = [descriptorCustody(host), descriptorCustody(host.sessions), descriptorCustody(host.composer), descriptorCustody(host.diagnostics), descriptorCustody(host.state)]
  const names = Reflect.ownKeys(host.state)

  for (let i = 0; i < names.length; i++) {
    const value = Object.getOwnPropertyDescriptor(host.state, names[i])?.value

    if (value && typeof value === 'object') {
      // Atom values/subscriber bookkeeping legitimately change; only operation doors are custody.
      checks[checks.length] = descriptorCustody(value, ['get', 'listen', 'subscribe'])
    }
  }

  return () => {
    for (let i = 0; i < checks.length; i++) {
      if (!checks[i]()) { return false }
    }

    return true
  }
}
