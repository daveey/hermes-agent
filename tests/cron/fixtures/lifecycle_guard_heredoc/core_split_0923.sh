#!/usr/bin/env bash
# Read-only: core vs graphql request counts per host from each host's github-quota audit.sqlite `requests` table
# for the quota window ending at reset 1794501882 (15:44:42-16:44:42Z = 07:44:42-08:44:42 PST).
set -u
cd "$(dirname "$0")"
END=1794501882; START=$((END-3600))
REMOTE='import os, sqlite3, socket, sys
p = os.path.expanduser("~/.hermes/logs/github-quota/audit.sqlite")
s, e = int(sys.argv[1]), int(sys.argv[2])
try:
    c = sqlite3.connect("file:" + p + "?mode=ro", uri=True, timeout=10)
    rows = c.execute("select coalesce(rate_limit_resource, \"(none)\"), count(*) from requests where started >= ? and started < ? group by 1 order by 1", (s, e)).fetchall()
    print(socket.gethostname(), dict(rows))
except Exception as x:
    print(socket.gethostname(), "ERR", type(x).__name__, str(x)[:120])'
run() { # $1 label, $2 ip, $3 extra opts
  /opt/homebrew/bin/timeout 40 ssh -o BatchMode=yes -o ConnectTimeout=8 $3 ubuntu@$2 "timeout 25 nice python3 -c '$(printf "%s" "$REMOTE" | sed "s/'/'\\\\''/g")' $START $END" 2>&1 | grep -avE 'SSH route|ghp_|gho_|github_pat_|Bearer|TOKEN|SECRET|PASS|eyJ|sk-ant|oat01' | sed "s/^/$1 /" | cut -c1-220
}
run hub 100.104.201.88 ""
run w1 100.80.172.11 ""
run w2 100.75.131.87 ""
run w3 100.79.155.17 ""
run w4 100.97.140.22 ""
run w5 100.121.18.97 "-o UserKnownHostsFile=kh_w5 -o StrictHostKeyChecking=accept-new"
run w6 100.88.175.125 ""
date '+checked %H:%M:%S %Z'
