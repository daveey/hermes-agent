"""Shared unchanged data and fixtures for the session-alias test partitions."""

import json
import os
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.environments.base_session_env import _alias_names_printf, _split_alias_marker
from tools.terminal_tool_guards import _session_aliases_for_guard, gateway_lifecycle_block


_FN_BODY = "q() { python3 - <<'PY'\nbash -c 'hermes gateway \"restart\"'\nPY\n}\nq\n"


def _big(tmp_path):
    big = tmp_path / "huge.sh"
    big.write_text("# padding\n" * 130000 + 'hermes gateway "restart"\n')
    return big


_CARD_REPRO = (
    "ssh -o BatchMode=yes -o ConnectTimeout=15 ubuntu@100.104.201.88 'bash -ls' <<'EOF'\n"
    "python3 - <<'PY'\n"
    "import os\n"
    "print(\"nested ssh python heredoc ok:\", "
    "os.path.exists(os.path.expanduser(\"~/.hermes/kanban.db\")))\n"
    "PY\n"
    "EOF"
)


_STOCK_ALIASES = frozenset({"ls", "grep", "fgrep", "egrep", "ll", "la", "l", "alert"})


@pytest.fixture
def live_kanban_db(tmp_path, monkeypatch):
    """`~/.hermes/kanban.db` held open (tracked) in this process, as the gateway holds its own."""
    from hermes_cli.sqlite_safe_read import connect_tracked

    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    conn = connect_tracked(home / ".hermes" / "kanban.db")
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    try:
        yield home / ".hermes" / "kanban.db"
    finally:
        conn.close()


_NESTED_FOREIGN_SHELLS = [
    "bash -l <<'EOF'\nt ( bash {db} )\nEOF",  # the nit's exact shape
    "bash -i <<'EOF'\nt ( bash {db} )\nEOF",
    "bash -ls <<'EOF'\nt ( bash {db} )\nEOF",
    "bash --login <<'EOF'\nt ( bash {db} )\nEOF",
    "sh -l <<'EOF'\nt ( bash {db} )\nEOF",
    "zsh <<'EOF'\nt ( bash {db} )\nEOF",  # zsh sources ~/.zshenv even when non-interactive
    "ssh -o BatchMode=yes ubuntu@h 'bash -ls' <<'EOF'\nt ( bash {db} )\nEOF",
    "ssh ubuntu@h <<'EOF'\nt ( bash {db} )\nEOF",  # no remote command: a remote login shell
    "ssh ubuntu@h 'bash -s' <<'EOF'\nt ( bash {db} )\nEOF",  # the remote host's startup files
    "sudo -i <<'EOF'\nt ( bash {db} )\nEOF",
    "bash -l -c 't ( bash {db} )'",
    "bash -lc 't ( bash {db} )'",  # a clustered `-c` (#2494)
    "bash -ilc 't ( bash {db} )'",
    "bash -euo pipefail -lc 't ( bash {db} )'",
    "zsh -lc 't ( bash {db} )'",
    "bash -c -l 't ( bash {db} )'",
    "zsh -c 't ( bash {db} )'",
    "zsh -ec 't ( bash {db} )'",
    "bash -l <<'EOF'\npython3 - <<'PY'\nprint(1)\nPY\nt ( bash {db} )\nEOF",
    "bash -l {script}",
    "bash -l <<< 't ( bash {db} )'",
    "bash -l < {script}",
    "bash -l 0< {script}",
    "bash -l 0<<< 't ( bash {db} )'",
    "bash -l <> {script}",
    "runuser -c 't ( bash {db} )' bob",
    "runuser --command='t ( bash {db} )' bob",
    "su -c 't ( bash {db} )' bob",
    "doas -u bob -s <<'EOF'\nt ( bash {db} )\nEOF",
    "env BASH_ENV=x bash -c 't ( bash {db} )'",
    "BASH_ENV=x bash {script}",
    "runuser -l bob <<'EOF'\nt ( bash {db} )\nEOF",
    "doas -s <<'EOF'\nt ( bash {db} )\nEOF",
    "su --command='t ( bash {db} )' bob",
    "BASH_ENV=x bash -c 't ( bash {db} )'",
    "BASH_ENV=x bash <<< 't ( bash {db} )'",
    "BASH_ENV=x bash < {script}",
    "ENV=x sh -i -c 't ( bash {db} )'",
    # su-mode operands after the user, even past `--`, reach the shell (`bash -c CMD`).
    "su -- bob -c 't ( bash {db} )'",
    "runuser -- bob -c 't ( bash {db} )'",
    "su bob -c 't ( bash {db} )'",
    "su -lc 't ( bash {db} )' bob",
    "su -c't ( bash {db} )' bob",
    # getopt_long accepts unique long-option prefixes.
    "runuser --whitelist -u bob <<< 't ( bash {db} )'",
    "runuser --white -u bob <<< 't ( bash {db} )'",
    "runuser --supp -u bob <<< 't ( bash {db} )'",
    "runuser --gro -u bob <<< 't ( bash {db} )'",
    "runuser --comm='t ( bash {db} )' bob",
    "runuser --com 't ( bash {db} )' bob",
    "su --sess='t ( bash {db} )' bob",
    "doas -a style -s <<< 't ( bash {db} )'",
    # A stdin file fed to a login-shell launcher is that shell's script (review 5903452583 B1).
    "su -l bob < <(cat {script})",
    "while read l; do su -l bob; done < <(cat {script})",
    "bash -l < <(cat {script})",
    "su -l bob 0< <(cat {script})",
    "sudo -i < <(cat {script})",
    "ssh host < <(cat {script})",
    "su -l bob < <(cat < {script})",
    "su -l bob < <(cat <(cat {script}))",
    "{{ su -l bob; }} < <(cat {script})",
    "( bash -l ) < <(cat {script})",
    "su -l bob < {script}",
    "su - bob < {script}",
    "su bob < {script}",
    "su -l bob 0< {script}",
    "su -l bob <> {script}",
    "runuser -l bob < {script}",
    "runuser - bob < {script}",
    "doas -s < {script}",
    "doas -u bob -s < {script}",
    "sudo -i < {script}",
    "sudo -s < {script}",
    "sudo -u bob -i < {script}",
    "ssh host < {script}",
    "ssh -p 22 host bash -s < {script}",
    # A compound command's stdin redirection or heredoc reaches the login shell inside it
    # (PR #2911 review follow-up). Pre-existing: base walked none of these in nested scope.
    "{{ su -l bob; }} < {script}",
    "( su -l bob ) < {script}",
    "{{ su -l bob; }} <<< 't ( bash {db} )'",
    "( su -l bob ) <<< 't ( bash {db} )'",
    "{{ su -l bob; }} <<'EOF'\nt ( bash {db} )\nEOF",
    "{{ su -l bob; }} <<-EOF\n\tt ( bash {db} )\n\tEOF",
    "if true; then su -l bob; fi < {script}",
    "if true; then su -l bob; fi <<'EOF'\nt ( bash {db} )\nEOF",
    "while read l; do su -l bob; done < {script}",
    "while read l; do su -l bob; done <<'EOF'\nt ( bash {db} )\nEOF",
    "for x in 1; do su -l bob; done < {script}",
    "case x in x) su -l bob;; esac < {script}",
    "{{ su -l bob; }} 0< {script}",
    "{{ su -l bob; }} <> {script}",
    "{{ true; {{ su -l bob; }}; }} < {script}",
    "{{\nsu -l bob\n}} < {script}",  # the group opens lines before its redirection
    "{{\nsu -l bob\n}} <<'EOF'\nt ( bash {db} )\nEOF",
    "{{ bash -l; }} < {script}",
    "{{ sudo -i; }} < {script}",
    "{{ doas -s; }} < {script}",
    "{{ ssh host; }} < {script}",
    "{{ su -l bob < /dev/null; }} < {script}",  # fails closed: own redirections are not tracked
    # A file piped into the login shell (t_01a1dae7a625be0c): base walked none of these in
    # nested scope, and handed a piped file to `su`/`sudo`/`ssh` in no scope at all.
    "cat {script} | su -l bob",
    "cat {script} | {{ su -l bob; }}",
    "cat {script} | ( su -l bob )",
    "cat {script} |& su -l bob",
    "cat {script} |\nsu -l bob",
    "cat < {script} | su -l bob",
    "cat {script} | bash -l",
    "cat {script} | sudo -i",
    "cat {script} | ssh host",
    "cat {script} | {{\nsu -l bob\n}}",
    "cat {script} | while read l; do\nsu -l bob\ndone",
    "(\nsu -l bob\n) < {script}",
    "while true; do\nsu -l bob\ndone < {script}",
    # Printed text a process substitution feeds the login shell's stdin, as the pipe twins
    # (`echo T | su -l bob`) are walked (PR #3368 review 5912629938; pre-existing on main).
    "su -l bob < <(echo 't ( bash {db} )')",
    "su -l bob < <(printf '%s\\n' 't ( bash {db} )')",
    "su -l bob 0< <(echo 't ( bash {db} )')",
    "< <(echo 't ( bash {db} )') su -l bob",
    "bash -l < <(echo 't ( bash {db} )')",
    "sudo -i < <(echo 't ( bash {db} )')",
    "ssh host < <(echo 't ( bash {db} )')",
    "su -l bob < <(echo ':'; echo 't ( bash {db} )')",
    "{{ su -l bob; }} < <(echo 't ( bash {db} )')",
    "( bash -l ) < <(printf '%s\\n' 't ( bash {db} )')",
    "while read l; do su -l bob; done < <(echo 't ( bash {db} )')",
    "su -l bob < <(python3 - <<'PY'\nprint('t ( bash {db} )')\nPY\n)",
    # The login shell behind a command wrapper (t_01a1db36142b6311): base read only the first
    # word, so `env`/`exec`/`command`/`nice`/`timeout` hid `su` and its stdin was never walked.
    "env su -l bob < {script}",
    "exec su -l bob < {script}",
    "exec -a x su -l bob < {script}",
    "exec -a x bash -l < {script}",
    "exec -a python3 bash -l < {script}",
    "exec -cl -a x bash -l < {script}",
    "command su -l bob < {script}",
    "nice su -l bob < {script}",
    "timeout 5 su -l bob < {script}",
    "env -i HOME=/x nohup nice -n 5 su -l bob < {script}",
    "sudo su -l bob < {script}",
    "sudo -u root su - bob < {script}",
    "env sudo -i < {script}",
    "nice doas -s < {script}",
    "command runuser -l bob < {script}",
    "{{ env su -l bob; }} < {script}",
    "cat {script} | env su -l bob",
    "cat {script} | exec su -l bob",
    "cat {script} | command su -l bob",
    "cat {script} | nice su -l bob",
    "cat {script} | timeout 5 su -l bob",
    "env su -l bob <<'EOF'\nt ( bash {db} )\nEOF",
    "nice su - bob <<< 't ( bash {db} )'",
    "timeout 5 su -c 't ( bash {db} )' bob",
    "env sudo -s <<'EOF'\nt ( bash {db} )\nEOF",
    # stdin duplicated from another descriptor (t_01a1daf4b486a755): the descriptor's source.
    "su -l bob 0<&3 3< {script}",
    "{{ su -l bob; }} 0<&3 3< {script}",
    "{{ su -l bob; }} <&3 3< {script}",
    "exec 3< {script}; {{ su -l bob; }} 0<&3",
    "exec 3< {script}\nsu -l bob <&3",
    "su -l bob < /dev/fd/3 3< {script}",
    "su -l bob < /proc/self/fd/3 3<> {script}",
    "su -l bob 0<&3 3<<< 't ( bash {db} )'",
    # A redirection on a call to a function the command defines reaches the body's shells.
    "f() {{ su -l bob; }}; f < {script}",
    "f() {{ su -l bob; }}; f <<< 't ( bash {db} )'",
    "f() {{ su -l bob; }}; f <<EOF\nt ( bash {db} )\nEOF",
    "function f {{ su -l bob; }}; f < {script}",
    "function f() {{ su -l bob; }}\nf 0< {script}",
    "f() {{\nsu -l bob\n}}\nf < {script}",
    "f() {{ su -l bob; }}; g() {{ f; }}; g < {script}",
    "f() {{ su -l bob; }}; f 0<&3 3< {script}",
    "f() ( su -l bob ); f < {script}",
    # A variable target resolved from the command's own assignments (t_01a1daf4b486a755).
    "S={script}; su -l bob < $S",
    "P={script}; ssh -o BatchMode=yes host 'bash -s' < $P",
    "D={tmp}; su -l bob < \"$D/inner.sh\"",
    "export S={script}; su -l bob < ${{S}}",
    "D={tmp}; P=$D/inner.sh; su -l bob < $P",
    "S={script}; exec 3< $S; su -l bob <&3",
    "S={script}\nfor h in a b; do ssh $h sh -s < $S; done",
]


_PY_PRINTS = "print('t ( bash {db} )')\n"


_PY_TAKEOVER = (
    "import os, sqlite3\n"
    "DB = os.path.expanduser('~/.hermes/kanban.db')\n"
    "con = sqlite3.connect(\"{db}\")\n"
    "print(con.execute('select 1').fetchall())\n"
)


_MULTI_HOP_DATA_CONSUMERS = [
    # The card's exact shape (2026-11-15 02:18 PST, David-MBP).
    "TZ=America/Los_Angeles date '+start %T %Z'\n"
    "ssh -o BatchMode=yes -o ConnectTimeout=20 ubuntu@100.104.201.88 \"ssh -o BatchMode=yes "
    "-o ConnectTimeout=15 hermes-worker-8 python3 -\" < {py} 2>&1 | grep -vE 'gh[ps]_|eyJ' "
    "| cut -c1-600\necho \"rc=${{PIPESTATUS[0]}}\"",
    "ssh h1 'ssh h2 python3 -' < {py}",
    "ssh h1 ssh h2 python3 - < {py}",
    "ssh -p 22 h1 \"ssh -J j h2 'timeout 20 python3 -'\" < {py}",
    "ssh h1 \"ssh h2 'ssh h3 python3 -'\" < {py}",
    "ssh h1 'ssh h2 cat' < {script}",
    # OpenSSH resumes option parsing after the destination (review 5911427581 B1).
    "ssh h1 'ssh h2 -p 22 python3 -' < {py}",
    "ssh h1 -T 'ssh h2 -o BatchMode=yes -T python3 -' < {py}",
    "ssh h1 'ssh h2 -- python3 -' < {py}",
    # A -c payload whose own stdin reader is a data consumer stays data.
    "ssh h1 \"ssh h2 'bash -c \\\"python3 -\\\"'\" < {py}",
    "ssh h1 'bash -c \"exec python3 -\"' < {py}",
    "ssh h1 \"ssh h2 python3 -\" <<'PY'\n" + _PY_TAKEOVER + "PY",
    # Allowlisted consumers after commands that never read stdin.
    "ssh h1 \"ssh h2 'cd /tmp && python3 -'\" < {py}",
    "ssh h1 'ssh h2 python3.12 -' < {py}",
    "ssh h1 'ssh h2 tee /tmp/takeover.py' < {py}",
    # Python reading its program from stdin, with option flags that take no code (review
    # 5941029921 B1 keeps these as data; only -c/-m/script launchers fail closed).
    "ssh h1 'ssh h2 python3' < {py}",
    "ssh h1 'ssh h2 python3 -u -' < {py}",
    "ssh h1 'ssh h2 python3 -IB -X utf8 -W ignore -' < {py}",
    "ssh h1 'ssh h2 python3 -Wignore -Xdev -' < {py}",
]


_MULTI_HOP_SHELLS = [
    "ssh h1 'ssh h2 git apply' < {script}",
    "ssh h1 \"bash -c 'ssh h2 git apply'\" < {script}",
    "ssh h1 \"ssh h2 'bash -c git'\" < {script}",
    "ssh h1 \"bash -c 'git -c alias.apply=!bash apply'\" < {script}",
    "ssh h1 \"bash -c 'source /dev/stdin; git apply'\" < {script}",
    "ssh h1 'ssh h2' < {script}",  # the last hop has no command: a remote login shell
    "ssh h1 'ssh h2 bash -s' < {script}",
    "ssh h1 \"ssh h2 'bash -l'\" < {script}",
    "ssh h1 \"ssh h2 'ssh h3'\" < {script}",
    "ssh h1 'ssh h2 python3 -; bash' < {script}",  # any shell segment reading stdin counts
    "ssh h1 'sudo -i' < {script}",  # other hops are still not modelled: fail closed
    "ssh h1 'ssh h2 su -l bob' < {script}",
    "ssh h1 \"ssh h2 'ssh h3 \\\"ssh h4 'ssh h5 python3 -'\\\"'\" < {script}",  # hop cap: closed
    # Options after the destination are still ssh's own (review 5911427581 B1): the remote
    # command starts at the first operand after them; none left = a login shell.
    "ssh h1 'ssh h2 -p 22 sh -s' < {script}",
    "ssh h1 'ssh h2 -T' < {script}",
    "ssh h1 'ssh h2 -o BatchMode=yes -p22 -T bash' < {script}",
    "ssh h1 -T < {script}",
    "ssh h1 -p 22 -- < {script}",
    # A shell's -c payload is followed too: a payload that runs a stdin-reading shell
    # executes the file (review 5911756789 B1), on the first hop and on later ones.
    "ssh h1 'bash -c bash' < {script}",
    "ssh h1 \"ssh h2 'bash -c bash'\" < {script}",
    "ssh h1 \"ssh h2 'sh -c sh'\" < {script}",
    "ssh h1 \"ssh h2 'bash -c \\\"exec bash\\\"'\" < {script}",
    "ssh h1 \"ssh h2 'bash -c \\\"bash -c sh\\\"'\" < {script}",
    "ssh h1 \"ssh h2 'sh -c \\\"ssh h3\\\"'\" < {script}",
    "ssh h1 \"ssh h2 'bash -c \\\"python3 -; sh\\\"'\" < {script}",
    # A followed hop hands stdin to a command only when it is a known data consumer; anything
    # else (scripts on /dev/stdin, builtins that run code, launchers) is a shell reader
    # (review 5914322131 B1: base refused every one of these, so they must stay refused).
    "ssh h1 'ssh h2 bash /dev/stdin' < {script}",
    "ssh h1 'ssh h2 sh /dev/stdin' < {script}",
    "ssh h1 'ssh h2 source /dev/stdin' < {script}",
    "ssh h1 'ssh h2 . /dev/stdin' < {script}",
    "ssh h1 'ssh h2 eval bash' < {script}",
    "ssh h1 'ssh h2 exec -a x bash' < {script}",
    "ssh h1 'ssh h2 busybox sh' < {script}",
    "ssh h1 \"ssh h2 'bash -c \\\"source /dev/stdin\\\"'\" < {script}",
    "ssh h1 \"bash -c 'source /dev/stdin'\" < {script}",
    "ssh h1 'ssh h2 flock /tmp/l bash' < {script}",
    "ssh h1 'ssh h2 chroot / bash' < {script}",
    "ssh h1 'ssh h2 sg grp bash' < {script}",
    "ssh h1 \"ssh h2 'script -qc bash /dev/null'\" < {script}",
    "ssh h1 'ssh h2 docker exec -i c bash' < {script}",
    "ssh h1 'ssh h2 kubectl exec -i p -- bash' < {script}",
    "ssh h1 'ssh h2 xargs -0 bash' < {script}",
    "ssh h1 \"ssh h2 'cd /tmp && bash'\" < {script}",
    # Python is a data consumer only when stdin is its program: a -c/-m/script launcher may
    # hand stdin to a shell (`python3 -c 'os.execlp("bash", "bash")'`; review 5941029921 B1).
    "ssh h1 \"ssh h2 'python3 -c x'\" < {script}",
    "ssh h1 \"ssh h2 'python3 -Ic x'\" < {script}",
    "ssh h1 \"ssh h2 'python3 -u -c x -'\" < {script}",
    "ssh h1 \"ssh h2 'python3 -cx'\" < {script}",
    "ssh h1 'ssh h2 python3 -m pty' < {script}",
    "ssh h1 'ssh h2 python3 -mpty' < {script}",
    "ssh h1 'ssh h2 python3 run.py' < {script}",
    "ssh h1 'ssh h2 python3 -X utf8 run.py' < {script}",
    "ssh h1 'ssh h2 python3 --check-hash-based-pycs never -' < {script}",
    "ssh h1 \"bash -c 'python3 -c x'\" < {script}",
    "ssh h1 \"ssh h2 'bash -c \\\"python3 -m pty\\\"'\" < {script}",
]
