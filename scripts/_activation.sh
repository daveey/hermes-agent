# Sourced, never executed: how a POSIX shell gets the Hermes dev environment.
# `activate` (interactive, reversible) and `scripts/run-in-hermes-env` (a child
# process, one-way) both build on it; neither owns any of it.
#
#   hermes_sync REPO [FLAG]         bring the install up to date (setup-hermes.sh)
#   hermes_compose_env REPO DIALECT print the composed environment, `sh` or `fish`
#   hermes_apply_env SCRIPT         evaluate a composed `sh` script into this shell
#   hermes_activation_current REPO  is the inherited environment still REPO's?
#   hermes_ensure_env REPO          sync, compose and apply unless it is current
#   hermes_borrow_account_store REPO  provision a faked home from the account's
#                                   tool store (run_tests.sh, run-in-hermes-env)
#
# pm records, beside the installed-state file named by __HERMES_ACTIVATED, one
# stamp per dependency input carrying the exact mtime that input had when the
# install was last verified against it (pm.environments.record_activation_inputs).
# Any input whose mtime DIFFERS from its stamp (newer or older, since a branch
# switch can move it either way) means the inherited environment may not match
# its inputs. `-nt`/`-ot` are bash builtins, so the check costs no process
# spawn, and every successful sync re-records, so one re-sync settles it.
# Missing stamps or inputs, a dangling sentinel or a literal "1" all read as
# stale, and so does an install that did not build the test environment:
# every environment this library composes includes it.

# hermes_sync REPO [--test-environment[=EXTRAS]]: run setup in a child, so a
# failure cannot exit or half-change the caller's shell and setup never
# republishes launchers or shell config. Progress goes to stderr.
hermes_sync() {
    local repo="$1" test_environment="${2:---test-environment}"
    (
        unset PYTHONHOME PYTHONPATH VIRTUAL_ENV
        bash "$repo/setup-hermes.sh" --runtime-only "$test_environment"
    ) >&2
}

# hermes_bootstrap_python REPO: print the interpreter that can run pm before
# any dependency is importable. It only emits the environment; it installs nothing.
hermes_bootstrap_python() {
    local repo="$1" store candidate
    for candidate in "$repo/.venv/bin/python" "$repo/.venv/Scripts/python.exe" \
                     "$repo/venv/bin/python" "$repo/venv/Scripts/python.exe"; do
        [ -x "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
    done
    for store in "${HERMES_RUNTIME_DIR:-}" "$repo/../tools" "${HERMES_HOME:-$HOME/.hermes}/tools"; do
        [ -n "$store" ] || continue
        for candidate in "$store"/python-*/bin/python3 "$store"/python-*/python.exe \
                         "$store"/python-*/bin/python "$store"/python-*/bin/python.exe; do
            [ -x "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
        done
    done
    return 1
}

# hermes_borrow_account_store REPO: when this process's Hermes root is not the
# OS account's -- a faked HOME (worktree lanes, probe homes, a review card's
# scratch home) or a HERMES_HOME outside the account root (a card's fake home
# for a cloned checkout) -- export HERMES_RUNTIME_DIR (and, when it holds the
# pinned minor, UV_PYTHON_INSTALL_DIR) so provisioning reuses the account's
# tool store instead of downloading ~1.5 GB of tools into every temporary
# home, but only while that store already records REPO's pins
# (hermes_store_has_pins). Status 1 and nothing exported otherwise. The check
# runs unlocked before `pm install` takes the store's install lock, so it keeps
# runs that START on different pins apart; it does not cover the window in
# between, where the account's own `hermes update` (or a run on other pins) can
# re-point facts.json and this run's `pm install` then re-stages its pins over
# them. An explicit HERMES_RUNTIME_DIR, the account's own root (or a profile of
# it), a root that already holds a store, or an account without a usable store
# keep the per-home layout; tool_store_pins.py resolves the root exactly as PM
# does. scripts/run_tests.sh and scripts/run-in-hermes-env both call it.
hermes_borrow_account_store() {
    local repo="$1" account py_minor
    [ -z "${HERMES_RUNTIME_DIR:-}" ] || return 1
    hermes_account_seed_enabled || return 1
    account="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)"
    [ -n "$account" ] || account="$(dscl . -read "/Users/$(id -un)" NFSHomeDirectory 2>/dev/null | sed -n 's/^NFSHomeDirectory: *//p')"
    [ -n "$account" ] && [ -d "$account" ] || return 1
    # Fast path for the owner's own runs, which never borrow: no Python spawn.
    if [ -z "${HERMES_HOME:-}" ]; then
        [ ! "$account" -ef "$HOME" ] || return 1
    else
        [ ! "$HERMES_HOME" -ef "$account/.hermes" ] || return 1
    fi
    [ -f "$account/.hermes/tools/facts.json" ] && [ -w "$account/.hermes/tools" ] || return 1
    py_minor="$(hermes_store_has_pins "$repo" "$account/.hermes/tools")" || return 1
    export HERMES_RUNTIME_DIR="$account/.hermes/tools"
    # setup-hermes.sh's uv-managed bootstrap interpreter is the same kind of
    # cache, but only reused: a missing pinned minor would download into it.
    if [ -z "${UV_PYTHON_INSTALL_DIR:-}" ] && [ -w "$account/.local/share/uv/python" ] \
        && compgen -G "$account/.local/share/uv/python/cpython-$py_minor[.-]*" >/dev/null; then
        export UV_PYTHON_INSTALL_DIR="$account/.local/share/uv/python"
    fi
    echo "▶ borrowed Hermes root: using the account's tool store $HERMES_RUNTIME_DIR" >&2
}

# hermes_account_seed_enabled: the same opt-out as
# pm.environments.account_seed_enabled (pm/AGENTS.md), which strips
# surrounding whitespace and ignores case before matching.
hermes_account_seed_enabled() {
    local value="${HERMES_PM_ACCOUNT_SEED:-}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$value" in 0|[Ff][Aa][Ll][Ss][Ee]|[Nn][Oo]|[Oo][Ff][Ff]) return 1 ;; esac
}

# hermes_store_has_pins REPO STORE: check, read-only for STORE, that it already
# records REPO's pins (scripts/tool_store_pins.py says why and what counts, and
# the one write: the borrower's own decline record); prints the Python minor.
hermes_store_has_pins() {
    local python
    python="$(HERMES_RUNTIME_DIR="$2" hermes_bootstrap_python "$1")" || return 1
    PYTHONHOME= "$python" -I -B "$1/scripts/tool_store_pins.py" "$1" "$2" 2>/dev/null
}

# hermes_compose_env REPO DIALECT: print the environment of the installed state
# as a script for a shell of DIALECT (`sh` or `fish`).
hermes_compose_env() {
    local repo="$1" dialect="$2" python script
    python="$(hermes_bootstrap_python "$repo")" || {
        echo "no bootstrap Python found; run setup-hermes.sh" >&2
        return 1
    }
    script="$(PYTHONHOME= PYTHONPATH="$repo" "$python" -m pm.environments --format "$dialect")" &&
        [ -n "$script" ] || {
        echo "could not read pm env (run ./setup-hermes.sh first)" >&2
        return 1
    }
    printf '%s\n' "$script"
}

# hermes_apply_env SCRIPT: evaluate a script from `hermes_compose_env REPO sh`.
hermes_apply_env() {
    # Resolved before the eval replaces PATH.
    local cygpath name
    cygpath="$(command -v cygpath 2>/dev/null || :)"
    eval "$1"
    # The MSYS/Cygwin runtime hands a native Windows Python these variables in
    # Windows form (`C:\a;C:\b`) and converts them back only for its own
    # children. Exported verbatim, bash splits PATH on ':' and finds no commands.
    if [ -n "$cygpath" ]; then
        PATH="$("$cygpath" -u -p "$PATH")"
        for name in HOME TMPDIR TMP TEMP; do
            if [ -n "${!name-}" ]; then
                printf -v "$name" '%s' "$("$cygpath" -u "${!name}")"
            fi
        done
    fi
}

# hermes_activation_current REPO: status 0 when the inherited environment
# matches REPO's current inputs.
hermes_activation_current() {
    local repo="$1" sentinel stamps stamp input stamped=0
    [ -n "${__HERMES_ACTIVATED:-}" ] && [ -e "${__HERMES_ACTIVATED}" ] || return 1
    # activate.ps1 records a Windows path; Git Bash accepts it with slashes.
    sentinel="${__HERMES_ACTIVATED//\\//}"
    stamps="${sentinel%/*}/inputs"
    [ -f "$stamps/.project-root" ] && [ -f "$stamps/.test-environment" ] || return 1
    local owner="$repo" recorded
    recorded="$(< "$stamps/.project-root")"
    if command -v cygpath >/dev/null 2>&1; then
        owner="$(cygpath -am "$repo")" || return 1
        recorded="${recorded//\\//}"
        [ "${owner,,}" = "${recorded,,}" ] || return 1
    else
        [ "$(cd "$owner" && pwd -P)" = "$recorded" ] || return 1
    fi
    for stamp in "$stamps"/* "$stamps"/*/*; do
        [ -f "$stamp" ] || continue
        stamped=1
        input="$repo/${stamp#"$stamps"/}"
        if [ ! -e "$input" ] || [ "$input" -nt "$stamp" ] || [ "$input" -ot "$stamp" ]; then
            return 1
        fi
    done
    [ "$stamped" = 1 ]
}

# hermes_ensure_env REPO: leave this shell with REPO's environment, doing
# nothing when the inherited one is current.
hermes_ensure_env() {
    local repo="$1" script
    hermes_activation_current "$repo" && return 0
    hermes_sync "$repo" || {
        echo "setup failed" >&2
        return 1
    }
    script="$(hermes_compose_env "$repo" sh)" || return 1
    hermes_apply_env "$script"
}
