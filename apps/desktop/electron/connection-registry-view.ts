export function createRegistryConnectionSanitizer(deps: {
  decrypt: (token: unknown) => string
  preview: (token: string) => string | null
  installId: (id: string) => string | undefined
}) {
  return (entry: Record<string, any>) => {
    const { token, headers, ...rest } = entry
    const decrypted = deps.decrypt(token)
    const knownInstallId = deps.installId(entry.id)

    return {
      ...rest,
      tokenSet: Boolean(decrypted),
      tokenPreview: deps.preview(decrypted),
      ...(knownInstallId ? { installId: knownInstallId } : {}),
      // Header values and token envelopes never cross the renderer IPC boundary.
      headerNames: headers && typeof headers === 'object' ? Object.keys(headers) : []
    }
  }
}
