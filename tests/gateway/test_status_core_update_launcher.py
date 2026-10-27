"""[daveey] gateway.status never takes the core-update launcher's process line for the gateway."""
from gateway import status


class TestCoreUpdateLauncherIsNotTheGateway:
    """[daveey] The core-update launcher carries the gateway argv after ``--`` and spawns it; only the
    gateway's own line may match, or stop/status/update would signal or count the launcher."""

    LAUNCHER = "/Users/u/.hermes/bin/hermes-gateway-launcher"
    TAIL = ["--home", "/Users/u/.hermes", "--", "/Users/u/.hermes/hermes-agent/.hermes/bin/hermes",
            "--run-module", "hermes_cli.stderr_timestamp", "--error-log", "/tmp/e.log", "--",
            "/Users/u/.hermes/hermes-agent/.hermes/bin/hermes", "gateway", "run", "--external-supervisor"]

    def test_launcher_exec_line_is_not_a_gateway(self):
        assert status.looks_like_gateway_command_line(" ".join([self.LAUNCHER, *self.TAIL])) is False

    def test_launcher_under_interpreter_is_not_a_gateway(self):
        line = " ".join(["/usr/local/bin/python3", self.LAUNCHER, *self.TAIL])
        assert status.looks_like_gateway_command_line(line) is False

    def test_gateway_child_line_still_matches(self):
        line = "/Users/u/.hermes/hermes-agent/.hermes/bin/hermes gateway run --external-supervisor"
        assert status.looks_like_gateway_command_line(line) is True
