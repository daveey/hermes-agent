/** Owner-qualified identity for session list rows. Stored ids are only unique per profile AND per
 *  gateway: two gateways may both serve `kanban_owner_terminal`. A row's canonical owner is
 *  `outcome.status.identity.gateway` (a connection id is only an alias of a gateway, so it never
 *  separates rows). Legacy rows with no canonical gateway keep the old profile + id collapse.
 *  Electron twin: electron/session-owner-identity.ts (the two trees cannot import each other). */

function field(value: unknown, key: string): unknown {
  return value && typeof value === 'object' ? (value as Record<string, unknown>)[key] : undefined
}

export function canonicalGatewayOf(row: unknown): string {
  const gateway = field(field(field(field(row, 'outcome'), 'status'), 'identity'), 'gateway')

  return typeof gateway === 'string' ? gateway.trim() : ''
}

/** Set of profile + id (+ canonical gateway when known). A row matches an entry of the same
 *  profile + id unless both name a canonical gateway and the gateways differ. */
export class SessionOwnerSet {
  private readonly byKey = new Map<string, Set<string>>()

  private key(profile: string, id: string): string {
    return `${profile}\0${id}`
  }

  has(profile: string, id: string, row: unknown): boolean {
    const owners = this.byKey.get(this.key(profile, id))
    const gateway = canonicalGatewayOf(row)

    return Boolean(owners && (!gateway || owners.has('') || owners.has(gateway)))
  }

  add(profile: string, id: string, row: unknown): void {
    const key = this.key(profile, id)
    const owners = this.byKey.get(key) ?? new Set<string>()
    owners.add(canonicalGatewayOf(row))
    this.byKey.set(key, owners)
  }
}

/** The row in `candidates` (all sharing one profile + id) that is the same owner as `row`: an exact
 *  canonical-gateway match wins, else a candidate that does not conflict (legacy fallback). */
export function sameOwnerRow<T>(candidates: readonly T[] | undefined, row: unknown): T | undefined {
  const gateway = canonicalGatewayOf(row)

  return (
    candidates?.find(c => gateway !== '' && canonicalGatewayOf(c) === gateway) ??
    candidates?.find(c => !gateway || !canonicalGatewayOf(c))
  )
}
