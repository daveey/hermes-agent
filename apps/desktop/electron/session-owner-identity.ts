/** Owner-qualified identity for session list rows. Stored ids are only unique per profile AND per
 *  gateway: two gateways may both serve `kanban_owner_terminal`. A row's canonical owner is
 *  `outcome.status.identity.gateway`; a connection only aliases a gateway, so it separates rows
 *  only when the gateways also differ. Legacy rows with no canonical gateway keep the old
 *  profile + id collapse.
 *  Renderer twin: src/lib/session-owner-identity.ts (electron cannot import from src). */

function field(value: unknown, key: string): unknown {
  return value && typeof value === 'object' ? (value as Record<string, unknown>)[key] : undefined
}

export function canonicalGatewayOf(row: unknown): string {
  const gateway = field(field(field(field(row, 'outcome'), 'status'), 'identity'), 'gateway')

  return typeof gateway === 'string' ? gateway.trim() : ''
}

function connectionOf(row: unknown): string {
  // Route carry preserves the source list separately from the route used to open it.
  const source = field(row, '_list_connection_id')
  const id = typeof source === 'string' ? source : field(row, 'connection_id')

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
