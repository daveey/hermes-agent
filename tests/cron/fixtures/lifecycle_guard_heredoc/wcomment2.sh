#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"
IP=$1; T=$2; F=$3
SSH="/opt/homebrew/bin/timeout 60 ssh -o BatchMode=yes -o ConnectTimeout=8 ${XOPTS:-} ubuntu@$IP"
$SSH "cat > /tmp/wc_$T.txt" < "$F"
$SSH "T=$T bash -s" <<'REMOTE' 2>&1 | grep -avE 'ghp_|gho_|github_pat_|sk-|xox|cfut_|Bearer|TOKEN|SECRET|PASS|access_token|refresh_token|eyJ|oat01|sk-ant'
set -u
HB=$(command -v hermes || ls ~/.local/bin/hermes 2>/dev/null || echo ~/.hermes/hermes-agent/venv/bin/hermes)
q() { timeout 20 python3 - "$1" <<'PY'
import sqlite3, sys, os, time
c = sqlite3.connect("file:" + os.path.expanduser("~/.hermes/kanban.db") + "?mode=ro", uri=True, timeout=10)
t = sys.argv[1]
r = c.execute("select status from tasks where id=?", (t,)).fetchone()
n = c.execute("select count(*), max(id) from task_comments where task_id=?", (t,)).fetchone()
print("status", r, "comments", n, time.strftime("%H:%M:%SZ", time.gmtime()))
PY
}
echo "before:"; q $T
env -u HERMES_KANBAN_TASK HERMES_AGENT=1 HERMES_SESSION_ID=20261026_235722_bef6c6 timeout 60 "$HB" kanban comment $T "$(cat /tmp/wc_$T.txt)" 2>&1 | tail -3 | cut -c1-200
echo "after:"; q $T
timeout 20 python3 - "$T" <<'PY'
import sqlite3, os, sys
c = sqlite3.connect("file:" + os.path.expanduser("~/.hermes/kanban.db") + "?mode=ro", uri=True, timeout=10)
cols = [r[1] for r in c.execute("pragma table_info(task_comments)")]
r = c.execute("select * from task_comments where task_id=? order by id desc limit 1", (sys.argv[1],)).fetchone()
d = dict(zip(cols, r))
print({k: (str(v)[:100] if k == "body" else v) for k, v in d.items()})
PY
rm -f /tmp/wc_$T.txt
REMOTE
