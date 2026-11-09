import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { load } from 'js-yaml';
import { expect, test } from 'vitest';

const root = fileURLToPath(new URL('../', import.meta.url));
const workflow = load(readFileSync(join(root, '.github/workflows/js-tests.yml'), 'utf8'));
const step = workflow.jobs.check.steps.find((entry) => entry.name === 'Run all workspace checks');

// Execute the workflow itself: a temporary root must support nested Unix sockets,
// executable helpers, cleanup, and a failing check's original exit status.
test.runIf(process.platform === 'linux')('disk fallback keeps fixture sockets below AF_UNIX limits', () => {
  const scratch = mkdtempSync(join(tmpdir(), 'temp-shell-'));
  try {
    const bin = join(scratch, 'bin');
    mkdirSync(bin);
    writeFileSync(join(bin, 'sudo'), '#!/bin/sh\nexit 1\n', { mode: 0o755 });
    // Force the noexec-shm branch, independent of this machine's shm policy.
    writeFileSync(join(bin, 'grep'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
    const runnerTemp = join(scratch, 'opt/runner/_work/_temp');
    mkdirSync(runnerTemp, { recursive: true });
    const selected = join(scratch, 'selected');
    const probe = `
      printf '%s' "$temp_root" > "$SELECTED"
      printf '#!/bin/sh\\nexit 0\\n' > "$temp_root/helper"
      chmod +x "$temp_root/helper"
      "$temp_root/helper"
      TMPDIR="$temp_root" node --input-type=module -e '
        import { tmpdir } from "node:os";
        import { mkdirSync } from "node:fs";
        import { createServer } from "node:net";
        const path = tmpdir() + "/vitest-MadqlD/desktop-gw-1YBcT6/hermes-gw-f639440fe47be53d/control.sock";
        mkdirSync(path.slice(0, path.lastIndexOf("/")), { recursive: true });
        const server = createServer();
        server.on("error", () => process.exit(3));
        server.listen(path, () => server.close());
      '
      exit 7
    `;
    // The probe stands in for the workflow's real workload line. If that line moves
    // (upstream rewrote it to `just check-js-ci`), a silent no-op replace would run
    // every real workspace check here, and spawnSync's timeout kills only bash, leaving
    // that duplicate suite running beside CI's own checks. Fail before spawning instead.
    const workload = 'TMPDIR="$temp_root" just check-js-ci';
    expect(step.run.split(workload).length - 1, `workflow step must run exactly one \`${workload}\``).toBe(1);
    const script = step.run.replace(workload, probe);
    const result = spawnSync('bash', ['-e', '-o', 'pipefail', '-c', script], {
      cwd: root,
      env: { ...process.env, RUNNER_TEMP: runnerTemp, SELECTED: selected, PATH: `${bin}:${process.env.PATH}` },
      encoding: 'utf8',
      timeout: 20_000,
    });
    expect(result.status, result.stdout + result.stderr).toBe(7);
    expect(existsSync(readFileSync(selected, 'utf8'))).toBe(false);
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
});
