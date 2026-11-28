/** Load-time served bytes, not a claim that a later disk copy was evaluated.
 * The reviewed manifest's static closure covers the chunk containing runtime.ts
 * (including bundler wrappers). Lazy chunks are deliberately outside this proof.
 */
export interface ByteIdentity {
  sha256: string
  byteLength: number
}
export interface ServedAsset extends ByteIdentity {
  url: string
}

export async function identifyBytes(source: string): Promise<ByteIdentity> {
  return identifyRawBytes(new TextEncoder().encode(source))
}

async function identifyRawBytes(bytes: Uint8Array<ArrayBuffer>): Promise<ByteIdentity> {
  const digest = await crypto.subtle.digest('SHA-256', bytes)

  return {
    sha256: Array.from(new Uint8Array(digest), n => n.toString(16).padStart(2, '0')).join(''),
    byteLength: bytes.byteLength
  }
}

export async function readServedAssets(root: string): Promise<ServedAsset[]> {
  const rootUrl = new URL(root)

  if (!['http:', 'https:', 'file:'].includes(rootUrl.protocol) || !rootUrl.pathname.includes('/assets/')) {
    throw new Error('UNBUNDLED_SDK_ASSETS')
  }

  const base = new URL('../', rootUrl)
  const signal = AbortSignal.timeout(5_000)
  const manifestUrl = new URL('renderer-manifest.json', base).href

  const read = async (url: string) => {
    const response = await fetch(url, { cache: 'no-store', credentials: 'same-origin', signal })

    if (!response.ok || (response.url && response.url !== url)) {
      throw new Error('SERVED_ASSET_READ_FAILED')
    }

    return new Uint8Array(await response.arrayBuffer())
  }

  const manifestBytes = await read(manifestUrl)
  const manifest = JSON.parse(new TextDecoder().decode(manifestBytes)) as Record<
    string,
    { file: string; imports?: string[] }
  >
  const rootKey = Object.keys(manifest).find(key => new URL(manifest[key].file, base).href === rootUrl.href)

  if (!rootKey) {
    throw new Error('SDK_ASSET_NOT_IN_MANIFEST')
  }
  const seen = new Set<string>()
  const urls = new Set<string>()

  const visit = (key: string) => {
    if (seen.has(key)) {
      return
    }

    if (seen.size >= 256) {
      throw new Error('SERVED_ASSET_GRAPH_LIMIT')
    }
    seen.add(key)
    const item = manifest[key]

    if (!item || !/^assets\/[^?#\\]+\.js$/.test(item.file) || item.file.split('/').includes('..')) {
      throw new Error('INVALID_SERVED_ASSET_GRAPH')
    }

    const url = new URL(item.file, base)

    urls.add(url.href)

    for (const dependency of item.imports ?? []) {
      visit(dependency)
    }
  }

  visit(rootKey)
  const assets: ServedAsset[] = [{ url: manifestUrl, ...(await identifyRawBytes(manifestBytes)) }]

  for (const url of [...urls].sort()) {
    assets.push({ url, ...(await identifyRawBytes(await read(url))) })
  }

  return assets
}
