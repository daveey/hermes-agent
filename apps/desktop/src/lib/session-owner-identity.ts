/** Owner-qualified identity for session list rows. Stored ids are only unique per profile AND per
 *  gateway: two gateways may both serve `kanban_owner_terminal`. A row's canonical owner is
 *  `outcome.status.identity.gateway`; a connection only aliases a gateway, so it separates rows
 *  only when the gateways also differ. Legacy rows with no canonical gateway keep the old
 *  profile + id collapse.
 *  Electron twin: electron/session-owner-identity.ts (the two trees cannot import each other). */

function field(value: unknown, key: string): unknown {
  return value && typeof value === 'object' ? (value as Record<string, unknown>)[key] : undefined
}

export function canonicalGatewayOf(row: unknown): string {
  const gateway = field(field(field(field(row, 'outcome'), 'status'), 'identity'), 'gateway')

  return typeof gateway === 'string' ? gateway.trim() : ''
}

function connectionOf(row: unknown): string {
  const id = field(row, 'connection_id')

  return typeof id === 'string' ? id.trim() : ''
}

interface OwnerTag {
  connection: string
  gateway: string
}

/** Two rows are different owners only when both name a canonical gateway, the names differ AND they
 *  arrive over different connections (untagged primary rows share one empty connection). The
 *  envelope gateway string of one connection changes over time (hub enrolment, rename, `local`
 *  fallback), so it must never split one connection's row from itself. */
function conflicts(a: OwnerTag, b: OwnerTag): boolean {
  return a.gateway !== '' && b.gateway !== '' && a.gateway !== b.gateway && a.connection !== b.connection
}

function ownerTagOf(row: unknown): OwnerTag {
  return { connection: connectionOf(row), gateway: canonicalGatewayOf(row) }
}

/** Set of profile + id (+ owner tag when known). A row matches an entry of the same profile + id
 *  unless {@link conflicts}. */
export class SessionOwnerSet {
  private readonly byKey = new Map<string, OwnerTag[]>()

  private key(profile: string, id: string): string {
    return `${profile}\0${id}`
  }

  has(profile: string, id: string, row: unknown): boolean {
    const tag = ownerTagOf(row)

    return (this.byKey.get(this.key(profile, id)) ?? []).some(owner => !conflicts(owner, tag))
  }

  add(profile: string, id: string, row: unknown): void {
    const key = this.key(profile, id)
    this.byKey.set(key, [...(this.byKey.get(key) ?? []), ownerTagOf(row)])
  }
}

/** The row in `candidates` (all sharing one profile + id) that is the same owner as `row`: an exact
 *  canonical-gateway match wins, else a same-connection candidate, else any non-conflicting one. */
export function sameOwnerRow<T>(candidates: readonly T[] | undefined, row: unknown): T | undefined {
  const tag = ownerTagOf(row)
  const ok = (c: T) => !conflicts(ownerTagOf(c), tag)

  return (
    candidates?.find(c => tag.gateway !== '' && ownerTagOf(c).gateway === tag.gateway) ??
    candidates?.find(c => ok(c) && ownerTagOf(c).connection === tag.connection) ??
    candidates?.find(ok)
  )
}
