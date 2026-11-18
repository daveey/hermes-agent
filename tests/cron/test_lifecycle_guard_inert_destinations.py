"""Keep inert write operands from becoming scripts at an unrelated shell sink."""
import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle


@pytest.mark.parametrize("writer", [
    'ln -s source "./target"',
    'printf "%s\\n" x > "./target" && [ -s "./target" ]',
])
def test_inert_destination_with_unrelated_shell_sink(tmp_path, writer):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")
    (tmp_path / "source").write_text("touch executed\n")
    command = writer + '\nprintf "true\\n" | bash'
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    result = subprocess.run(
        [bash, "-c", command], cwd=tmp_path, capture_output=True, text=True,
        timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "executed").exists()
    assert not unsafe, reason
