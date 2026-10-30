#!/usr/bin/env bash
# Behaviour tests for ghr-imds-hook / ghr-start with stubbed curl + aws. No AWS needed.
# Run: bash hosts/hermes-ci-runners/tests/test_imds_hook.sh
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
FILES=$HERE/../packer/files
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin"
fail=0
ok()  { echo "PASS $1"; }
bad() { echo "FAIL $1"; fail=1; }

cat > "$T/bin/curl" <<'EOF'
#!/usr/bin/env bash
args="$*"
# A disabled IMDS answers HTTP 403 (it does not refuse the connection): exit 22 with -f, exit 0 without.
if [ -f "$STUB_STATE/disabled" ] && [ -z "${STUB_STILL_UP:-}" ]; then
  for a in "$@"; do case "$a" in -f|-f?*) exit 22;; esac; done
  echo Forbidden; exit 0
fi
case "$args" in
  *api/token*) echo tok; exit 0;;
esac
case "$args" in
  *meta-data/instance-id*) echo i-0abc;;
  *placement/region*) echo us-west-2;;
  *security-credentials/\ *|*security-credentials/) echo role;;
  *security-credentials/role*) echo '{"AccessKeyId":"AK","SecretAccessKey":"SK","Token":"ST"}';;
  *tags/instance/ghr:ssm_config_path*) echo /ghr/cfg;;
  *) echo ok;;
esac
EOF
cat > "$T/bin/aws" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  *get-parameters-by-path*) echo '[{"Name":"/ghr/cfg/token_path","Value":"/ghr/tok"}]';;
  *"get-parameter "*) echo "JITCONFIG123";;
  *delete-parameter*) :;;
  *modify-instance-metadata-options*)
    [ -n "${STUB_MODIFY_FAIL:-}" ] && exit 254
    touch "$STUB_STATE/disabled";;
  *describe-instances*) echo "${STUB_READBACK:-disabled}";;
esac
EOF
chmod +x "$T/bin/"*

run_hook() { # name, env assignments...
  rm -rf "$T/state" "$T/out"; mkdir -p "$T/state" "$T/out"
  env PATH="$T/bin:$PATH" STUB_STATE="$T/state" GHR_JIT_OUT="$T/out/jit" "$@" bash "$FILES/ghr-imds-hook" >"$T/log" 2>&1
}

run_hook; rc=$?
[ $rc -eq 0 ] && [ "$(cat "$T/out/jit")" = JITCONFIG123 ] && ok "happy path writes JIT config" || bad "happy path rc=$rc"

run_hook STUB_READBACK=enabled; rc=$?
[ $rc -eq 11 ] && [ ! -e "$T/out/jit" ] && ok "readback=enabled fails closed, no JIT file" || bad "readback enabled rc=$rc"

run_hook STUB_STILL_UP=1; rc=$?
[ $rc -eq 12 ] && [ ! -e "$T/out/jit" ] && ok "IMDS still reachable fails closed" || bad "still up rc=$rc"

# The post-disable probe must use -f: grep the hook so a regression to a non -f probe is caught even if the stub changes.
grep -q 'curl -fsS -m 2 -o /dev/null -X PUT "$IMDS/latest/api/token"' "$FILES/ghr-imds-hook" && ok "data-plane probe uses curl -f on the token PUT" || bad "data-plane probe without -f"

run_hook STUB_MODIFY_FAIL=1; rc=$?
[ $rc -ne 0 ] && [ ! -e "$T/out/jit" ] && ok "modify call failure fails closed" || bad "modify fail rc=$rc"

# ghr-start: failing hook => poweroff, runner never launched
mkdir -p "$T/runner"; cat > "$T/runner/run.sh" <<EOF
#!/usr/bin/env bash
touch "$T/runner-started"
EOF
chmod +x "$T/runner/run.sh"
cat > "$T/bin/sudo" <<'EOF'
#!/usr/bin/env bash
shift 2; exec "$@"
EOF
cat > "$T/hook-bad" <<'EOF'
#!/usr/bin/env bash
exit 11
EOF
cat > "$T/hook-good" <<EOF
#!/usr/bin/env bash
echo CFG > "$T/jit"
EOF
cat > "$T/poweroff" <<EOF
#!/usr/bin/env bash
touch "$T/powered-off"
EOF
chmod +x "$T/bin/sudo" "$T/hook-bad" "$T/hook-good" "$T/poweroff"
start() { rm -f "$T/runner-started" "$T/powered-off" "$T/jit"; env PATH="$T/bin:$PATH" GHR_HOOK="$1" GHR_JIT_OUT="$T/jit" GHR_RUNNER_DIR="$T/runner" GHR_POWEROFF="$T/poweroff" bash "$FILES/ghr-start" >/dev/null 2>&1; }
start "$T/hook-bad"
[ ! -e "$T/runner-started" ] && [ -e "$T/powered-off" ] && ok "hook failure: runner not started, powered off" || bad "start with bad hook"
start "$T/hook-good"
[ -e "$T/runner-started" ] && [ -e "$T/powered-off" ] && [ ! -e "$T/jit" ] && ok "hook success: runner started, JIT file removed, powered off after" || bad "start with good hook"
exit $fail
