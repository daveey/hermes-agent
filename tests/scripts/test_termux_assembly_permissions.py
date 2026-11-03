"""Exercise the build mount's real Linux UID permissions, not Android binaries."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
# Only the stdlib facts publisher runs here; the bionic tool bytes are never
# executed. A native Linux image keeps this kernel-permissions test off QEMU.
IMAGE = "python@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a"

# Hung-process guards only: they return as soon as docker answers. A CPU-throttled
# hub runner can take well over 15s for `docker info`, so keep them generous.
PROBE_TIMEOUT = 120
RUN_TIMEOUT = 300


def _remove_mount_dir(path: Path) -> None:
    """Remove a per-test dir under the shared mount root, even if a container left it.

    A run that dies between ``prepare_assembly`` and ``restore_assembly_owner`` leaves
    the assembly owned by the container uid; hand it back via the same image first.
    """
    if not path.exists():
        return
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        subprocess.run(
            ["docker", "run", "--rm", "--user", "0:0", "--network", "none",
             "--entrypoint", "chown", "-v", f"{path}:/cleanup", IMAGE,
             "-R", f"{os.getuid()}:{os.getgid()}", "/cleanup"],
            capture_output=True, timeout=PROBE_TIMEOUT, check=False,
        )
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def payload(tmp_path, request):
    """A fresh payload dir the docker daemon can bind-mount.

    The docker daemon resolves bind-mount sources on ITS filesystem. On the
    docker-ci runner pytest's /var/tmp basetemp is not visible to the daemon (it
    mounts an empty dir, so chown /assembly/tools fails); the workflow points
    HERMES_TEST_DOCKER_MOUNT_ROOT at a runner-temp dir the daemon shares. That
    dir outlives a run on the long-lived runner, so each test gets its own
    ``mkdtemp`` dir there (never a name derived from ``tmp_path``, which repeats
    across runs) and removes it afterwards, pass or fail.
    """
    mount_root = os.environ.get("HERMES_TEST_DOCKER_MOUNT_ROOT")
    if not mount_root:
        path = tmp_path / "payload"
        path.mkdir()
        return path
    Path(mount_root).mkdir(parents=True, exist_ok=True)
    owned = Path(tempfile.mkdtemp(prefix=f"{tmp_path.name}-", dir=mount_root))
    request.addfinalizer(lambda: _remove_mount_dir(owned))
    path = owned / "payload"
    path.mkdir()
    return path


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("build_exit", [0, 23], ids=["success", "failed-build"])
def test_assembly_publishes_facts_as_termux_uid_and_returns_host_owned_output(payload, build_exit):
    if not shutil.which("docker"):
        pytest.skip("Docker is required for the bind-mount ownership regression")
    # A daemon that hangs past the probe timeout is as unavailable as one that refuses
    # (CI runner daemons do both); only the assembly run below may fail this test.
    try:
        available = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=PROBE_TIMEOUT,
            check=False,
        )
        if available.returncode:
            pytest.skip("Docker daemon is unavailable")
        image = subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=PROBE_TIMEOUT,
            check=False,
        )
        if image.returncode:
            pytest.skip(f"Pre-pull the native Linux test image: docker pull {IMAGE}")
    except subprocess.TimeoutExpired:
        pytest.skip(f"Docker daemon did not answer within {PROBE_TIMEOUT} s")

    for name in ("python", "uv"):
        entry = payload / name
        entry.mkdir()
        (entry / "bytes").write_bytes(b"read-only staged tool bytes")
    before = {path: (path.stat().st_uid, path.stat().st_gid, path.stat().st_mode)
              for path in payload.rglob("*")}
    result = subprocess.run(
        ["bash", "-c", r'''
set -euo pipefail
# Ubuntu's default user-private-group umask (CI runners use it): output must not inherit it.
umask 0002
IMAGE="$1"; PAYLOAD_ABS="$2"; REPO="$3"
source "$REPO/scripts/termux/assembly_permissions.sh"
prepare_assembly
trap restore_assembly_owner EXIT
docker run --rm --user 1000:1000 --network none \
    -v "$ASSEMBLY:/assembly" \
    -v "$PAYLOAD_ABS/python:/assembly/tools/python:ro" \
    -v "$PAYLOAD_ABS/uv:/assembly/tools/uv:ro" \
    -v "$REPO:/app:ro" \
    -e HERMES_HOME=/tmp/hermes \
    "$IMAGE" sh -ec '
        python -B /app/scripts/termux/build_environment.py prepare-tools \
            --root /assembly --source-tools /assembly/tools
        mkdir /assembly/venv /assembly/pm-runtime
        umask 077
        printf "private build output" > /assembly/pm-runtime/receipt
        stat -c "%u:%g" /assembly/tools/facts.json > /assembly/writer
        exit "$1"
    ' sh "$4"
''', "assembly-permissions-test", IMAGE, str(payload), str(ROOT), str(build_exit)],
        cwd=ROOT, capture_output=True, text=True, timeout=RUN_TIMEOUT,
        check=False,
    )
    assert result.returncode == build_exit, result.stdout + result.stderr
    assembly, = payload.glob(".environments-*")
    facts = json.loads((assembly / "tools/facts.json").read_text())
    assert set(facts["packages"]) == {"python", "uv"}
    assert (assembly / "writer").read_text().strip() == "1000:1000"
    assert (assembly / "pm-runtime/receipt").read_text() == "private build output"
    for path in [assembly, *assembly.rglob("*")]:
        info = path.stat()
        assert (info.st_uid, info.st_gid) == (os.getuid(), os.getgid()), path
        assert not info.st_mode & 0o022, path
    assert {path: (path.stat().st_uid, path.stat().st_gid, path.stat().st_mode)
            for path in before} == before
    shutil.rmtree(assembly)
