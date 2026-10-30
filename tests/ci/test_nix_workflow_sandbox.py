"""nix.yml must build every flake check inside a real Nix sandbox, never a silent unsandboxed fallback.

pm-packages-exec (nix/checks.nix) refuses to pass when the host /lib64 loader is visible to the builder.
With Nix's default sandbox-fallback the runner without namespace support builds unsandboxed, so that
guard failed with "sandbox unexpectedly carries a host loader" (PR254, docker-ci). The job runs on the
runner itself (no `container:`), whose rootless job container provides the namespaces.
"""

from pathlib import Path

from ruamel.yaml import YAML

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "nix.yml"


def _job():
    return YAML(typ="safe").load(WORKFLOW.read_text())["jobs"]["flake-check"]


def _nix_config():
    steps = [s for s in _job()["steps"] if str(s.get("uses", "")).startswith("cachix/install-nix-action@")]
    assert len(steps) == 1, steps
    settings = {}
    for line in steps[0]["with"]["extra_nix_config"].splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            settings[key.strip()] = value.strip()
    return settings


def test_flake_check_requires_the_sandbox():
    settings = _nix_config()
    assert settings.get("sandbox") == "true"
    assert settings.get("sandbox-fallback") == "false"
