import shared from '../eslint.config.shared.mjs'

export default [
  ...shared,
  {
    files: ['packages/hermes-ink/**/*.{ts,tsx}'],
    rules: {
      '@typescript-eslint/consistent-type-imports': 'off',
      'no-constant-condition': 'off',
      'no-empty': 'off',
      'no-redeclare': 'off',
      'react-hooks/exhaustive-deps': 'off'
    }
  },
  {
    // Physical flushes go through src/lib/durableFlush.ts, which test suites stub: a direct
    // fsync elsewhere makes renderer tests wait on the shared CI disk (5 s timeouts).
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['src/lib/durableFlush.ts', 'src/__tests__/durableFlush.test.ts'],
    rules: {
      'no-restricted-imports': ['error', {
        paths: ['fs', 'node:fs', 'fs/promises', 'node:fs/promises'].map(name => ({
          name,
          importNames: ['fsync', 'fsyncSync', 'fdatasync', 'fdatasyncSync'],
          message: 'Flush through src/lib/durableFlush.ts.'
        }))
      }],
      'no-restricted-syntax': ['error',
        { selector: "Property[key.name='flush'][value.value=true]", message: 'Flush through src/lib/durableFlush.ts.' },
        { selector: "CallExpression[arguments.length=0][callee.property.name=/^(sync|datasync)$/]", message: 'Flush through src/lib/durableFlush.ts.' },
        { selector: "CallExpression[callee.property.name=/^f(data)?sync(Sync)?$/]", message: 'Flush through src/lib/durableFlush.ts.' }]
    }
  }
]
