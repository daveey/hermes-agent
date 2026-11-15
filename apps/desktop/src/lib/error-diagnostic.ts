// Deliberately never stringify the thrown object: RPC envelopes can carry credentials,
// request bodies, cycles, accessors and user-defined toJSON implementations.
const MAX_TEXT = 8192
const MAX_DEPTH = 6
const MAX_NODES = 32

export function errorField(value: unknown, key: string): unknown {
  if (!value || typeof value !== 'object') {
    return undefined
  }

  try {
    return Object.getOwnPropertyDescriptor(value, key)?.value
  } catch {
    return undefined
  }
}

export function errorDiagnostic(error: unknown, fallback: string): { text: string; codes: string } {
  if (typeof error === 'string') {
    return { text: error, codes: '' }
  }

  if (error instanceof Error) {
    return { text: error.message, codes: '' }
  }

  const seen = new Set<object>()
  const codes: string[] = []

  function visit(value: unknown, depth: number): string | undefined {
    if (depth > MAX_DEPTH) {
      return undefined
    }

    if (typeof value === 'string') {
      return value.slice(0, MAX_TEXT).trim() || undefined
    }

    if (!value || typeof value !== 'object' || seen.has(value) || seen.size >= MAX_NODES) {
      return undefined
    }

    seen.add(value)
    const code = errorField(value, 'code')

    // Only classification tokens, not arbitrary fields or code payloads.
    if (
      typeof code === 'string' &&
      code.length <= 128 &&
      /^(storage_[a-z_]+|disk_full|gateway_auth_failed|invalid_api_key)$/.test(code)
    ) {
      codes.push(JSON.stringify({ code }))
    }

    let text: string | undefined

    // Inner diagnostic detail wins over generic RPC wrapper messages. Continue
    // the bounded walk to retain classification codes even after finding text.
    for (const key of ['detail', 'data', 'error', 'message']) {
      const candidate = visit(errorField(value, key), depth + 1)
      text ??= candidate
    }

    return text
  }

  const text = visit(error, 0) ?? fallback

  return { text, codes: codes.join(' ') }
}
