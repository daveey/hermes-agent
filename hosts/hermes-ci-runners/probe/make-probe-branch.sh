#!/usr/bin/env bash
# Creates a probe branch from main where the five target workflows run ONLY on the tfgar-probe
# lane. Never touches real labels on main. Usage: probe/make-probe-branch.sh [branch]
set -euo pipefail
# Switches branches in the current checkout: run it in a dedicated worktree, never in a pinned clone.
BR=${1:-tfgar-probe-run}
LBL='[self-hosted, linux, x64, tfgar-probe]'
git fetch -q origin main
git checkout -q -B "$BR" origin/main
git checkout -q tfgar-probe-runners -- hosts/hermes-ci-runners .github/workflows/tfgar-probe.yml
# Portable rewrites (python, not GNU sed: BSD sed on macOS silently skipped these).
python3 - "$LBL" <<'PY2'
import re, sys
lbl = sys.argv[1]
for f in ("tests", "rust-tests", "js-tests", "remote-backends"):
    p = f".github/workflows/{f}.yml"
    s = open(p).read()
    s = re.sub(r"runs-on: \[self-hosted, (hermes-hub|docker-ci)\]", f"runs-on: {lbl}", s)
    open(p, "w").write(s)
# remote-backends is workflow_call only: add a manual trigger.
p = ".github/workflows/remote-backends.yml"
s = open(p).read()
assert "\n  workflow_call:" in s, "remote-backends.yml has no workflow_call trigger"
s = s.replace("\n  workflow_call:", "\n  workflow_dispatch:\n  workflow_call:", 1)
open(p, "w").write(s)
PY2
# pm-bundle: the fork skips it and its linux legs are commented out. Probe one native linux-x64 leg.
python3 - <<'PY'
import re
p = ".github/workflows/pm-bundle.yml"
s = open(p).read()
s = s.replace("    if: github.repository != 'daveey/hermes'\n", "")
s = s.replace("          - label: darwin-arm64\n            runner: macos-15\n",
              "          - label: linux-x64\n            runner: [self-hosted, linux, x64, tfgar-probe]\n          - label: darwin-arm64\n            runner: macos-15\n")
open(p, "w").write(s)
PY
grep -n "tfgar-probe" .github/workflows/*.yml | head -20
echo "review pm-bundle matrix: drop non-linux legs by hand on the probe branch if the matrix still lists them"
