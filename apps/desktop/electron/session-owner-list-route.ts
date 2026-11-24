/** Owner lookup explicitly enumerates captured connections. Do not substitute
 * the best-effort sidebar union (which omits down connections) for that read.
 * The normal pinned backend path still authenticates and scopes every request. */
export function isMergeableSessionListRequest(method: string, pathname: string, params: URLSearchParams): boolean {
  return method === 'GET' && pathname === '/api/profiles/sessions' && params.get('archive_owner_lookup') !== '1'
}

export function parseSessionRequestPath(path: string): URL | null {
  try { return new URL(path, 'http://x') } catch { return null }
}
