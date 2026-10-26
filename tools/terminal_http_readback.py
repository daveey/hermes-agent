"""Opt-in, Linux-local HTTP status readback without a shell or inherited env.

Not a general argv executor. The ordinary terminal approval/pre-exec guards run
before this producer. Trust assumes the host OS/system curl and Python runtime;
this is execution provenance, not attestation against a compromised host.
"""
import json
import shlex
import subprocess
import time
from urllib.parse import urlsplit

from hermes_platform.host.facts import os_family
from tools.environments.local import build_subprocess_env
from tools.interrupt import is_interrupted

HTTP_READBACK_EXECUTION = "local-http-readback-v1"
_HTTP_PREFIX = ["/usr/bin/curl", "-q", "--noproxy", "*", "--path-as-is",
                "--max-time", "5", "--silent", "--show-error", "--output",
                "/dev/null", "--write-out", "%{http_code}", "--"]


def readback_argv(command, *, env_type, background, pty, promoted):
    """Reject unsupported requests before backend acquisition or shell startup."""
    if (os_family() != "linux" or env_type != "local" or background or pty or promoted):
        raise ValueError("http_readback requires Linux local foreground execution")
    tokens = shlex.split(command)
    if (tokens[:-1] != _HTTP_PREFIX or len(tokens) != len(_HTTP_PREFIX) + 1
            or shlex.join(tokens) != command):
        raise ValueError("http_readback requires the canonical bounded curl command")
    url = urlsplit(tokens[-1])
    if (url.scheme not in ("http", "https") or not url.netloc or url.fragment
            or url.username is not None or url.password is not None
            or any(ord(c) < 33 or ord(c) == 127 for c in tokens[-1])
            or any(c in tokens[-1] for c in "{}[]")):
        # curl URL globbing can make one argument produce multiple requests.
        raise ValueError("http_readback requires one literal HTTP(S) URL without credentials or globbing")
    return tokens


def run_http_readback(argv, *, timeout, cwd):
    """One direct exec, bounded wait, no retry; never parse authority from stdout."""
    result = {"output": "", "exit_code": None, "error": None}
    if is_interrupted():
        return json.dumps({**result, "exit_code": 130, "error": "interrupted"})
    # Explicit empty base excludes LD_PRELOAD, BASH_ENV, exported functions,
    # proxies, CURL_HOME and certificate overrides. Keep the central spawn policy.
    child_env = build_subprocess_env(base={"LANG": "C"}, inherit_profile_home=False,
                                     scrub_secrets=False)
    deadline = time.monotonic() + min(timeout, 6)
    try:
        with subprocess.Popen(argv, shell=False, env=child_env, cwd=cwd,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) as proc:
            while True:
                if is_interrupted() or time.monotonic() >= deadline:
                    proc.kill()
                    proc.wait(timeout=2)
                    interrupted = is_interrupted()
                    return json.dumps({**result, "exit_code": 130 if interrupted else 124,
                                       "error": "interrupted" if interrupted else "timed out"})
                try:
                    output, error = proc.communicate(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    continue
            result.update(output=output.decode("ascii", errors="replace"),
                          exit_code=proc.returncode,
                          error=error.decode("utf-8", errors="replace") or None,
                          execution=HTTP_READBACK_EXECUTION)
    except OSError as exc:
        result["error"] = str(exc)
    if result["error"]:
        from agent.redact import redact_sensitive_text
        result["error"] = redact_sensitive_text(result["error"], force=True)
    return json.dumps(result)
