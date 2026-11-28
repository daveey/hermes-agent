/** Captured before runtime plugin evaluation; no mutable host lookup grants authority. */
const keys = Reflect.ownKeys
const descriptor = Object.getOwnPropertyDescriptor
const descriptors = Object.getOwnPropertyDescriptors
const same = Object.is
const create = Object.create
const setPrototype = Object.setPrototypeOf
const array = Array.isArray
const stringify = JSON.stringify
const flags = ['configurable', 'enumerable', 'writable', 'value', 'get', 'set'] as const

export function descriptorMatches(
  before: PropertyDescriptor | undefined,
  after: PropertyDescriptor | undefined
): boolean {
  if (!before || !after) {
    return before === after
  }

  for (let i = 0; i < flags.length; i++) {
    if (!same(descriptor(before, flags[i])?.value, descriptor(after, flags[i])?.value)) {
      return false
    }
  }

  return true
}

/** Snapshot and comparison never invoke application accessors. */
export function descriptorCustody(target: object, selected?: readonly PropertyKey[]): () => boolean {
  const names = selected ?? keys(target)
  const before = descriptors(target)

  return () => {
    if (!selected && keys(target).length !== names.length) {
      return false
    }

    for (let i = 0; i < names.length; i++) {
      const name = names[i]

      if (!descriptorMatches(descriptor(before, name)?.value, descriptor(target, name))) {
        return false
      }
    }

    return true
  }
}

/** No inherited then/toJSON or array prototype hooks participate in transport. */
function safeValue(value: unknown): unknown {
  if (value === null || typeof value !== 'object') {
    return value
  }
  const out = (array(value) ? setPrototype([], null) : create(null)) as Record<PropertyKey, unknown>
  const names = keys(value)

  for (let i = 0; i < names.length; i++) {
    const name = names[i]

    if (name === 'length' && array(value)) {
      continue
    }
    const own = descriptor(value, name)

    if (own && 'value' in own) {
      out[name] = safeValue(own.value)
    }
  }

  return out
}

export function diagnosticJson(value: unknown): string {
  return stringify(safeValue(value))
}
