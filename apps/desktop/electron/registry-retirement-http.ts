interface RetirementReply {
  ok?: boolean
  idle?: boolean | null
  token?: string
  sshOwnerNonce?: string
  protocolVersion?: number
}

type RequestJson<D> = (
  descriptor: D,
  path: string,
  options: { method?: string; body?: Record<string, string>; timeoutMs: number }
) => Promise<unknown>

/** Remote descriptors must use the bearer-aware authenticated transport.
 * Unlike the local pool client, an unknown commit result must propagate: the
 * coordinator must retain its gate until the exact resident is reconciled.
 */
export function createRegistryRetirementClient<D>(requestJson: RequestJson<D>) {
  async function request(descriptor: D, action: string, token?: string) {
    return (await requestJson(descriptor, '/api/health/retirement', {
      method: 'POST',
      body: { action, ...(token ? { token } : {}) },
      timeoutMs: 3000
    })) as RetirementReply | null
  }

  return {
    proveOwned: async (descriptor: D, nonce: string): Promise<boolean> => {
      const proof = (await requestJson(descriptor, '/api/ssh/ownership', { timeoutMs: 3000 })) as RetirementReply | null

      // runtimeIntact is deliberately NOT a prerequisite for retirement. A
      // deleted managed Python runtime is precisely what reconnect repairs.
      return Boolean(nonce) && proof?.ok === true && proof.protocolVersion === 1 && proof.sshOwnerNonce === nonce
    },
    prepare: async (descriptor: D): Promise<string | null> => {
      const reply = await request(descriptor, 'prepare')

      return reply?.ok === true && reply.idle === true && typeof reply.token === 'string' && reply.token
        ? reply.token
        : null
    },
    commit: async (descriptor: D, token: string): Promise<boolean> => {
      const reply = await request(descriptor, 'commit', token)

      if (reply?.ok !== true && reply?.ok !== false) {
        throw new Error('Unknown retirement commit outcome')
      }

      return reply.ok
    },
    cancel: async (descriptor: D, token: string): Promise<void> => {
      await request(descriptor, 'cancel', token)
    }
  }
}
