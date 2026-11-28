import { expect, test } from 'vitest'

import { createRegistryConnectionSanitizer } from './connection-registry-view'

test('renderer view removes credential envelopes and header values and preserves display metadata', () => {
  const sanitize = createRegistryConnectionSanitizer({
    decrypt: token => (token ? 'synthetic-secret' : ''),
    preview: token => (token ? 'synthetic…' : null),
    installId: id => (id === 'env' ? 'installation' : undefined)
  })

  expect(
    sanitize({
      id: 'env',
      label: 'Environment gateway',
      token: { value: 'secret' },
      headers: { Authorization: 'header-secret' }
    })
  ).toEqual({
    id: 'env',
    label: 'Environment gateway',
    installId: 'installation',
    tokenSet: true,
    tokenPreview: 'synthetic…',
    headerNames: ['Authorization']
  })
  expect(sanitize({ id: 'local', kind: 'local' })).toEqual({
    id: 'local',
    kind: 'local',
    tokenSet: false,
    tokenPreview: null,
    headerNames: []
  })
})
