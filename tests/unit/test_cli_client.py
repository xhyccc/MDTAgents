"""
Unit tests for src/cli_client.py
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from src.cli_client import OpenCodeClient, AgentError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(tmp_path: Path) -> OpenCodeClient:
    return OpenCodeClient(error_log_dir=tmp_path / "errors", default_model="test-model")


def _make_completed_process(stdout: str = "ok", returncode: int = 0, stderr: str = "") -> MagicMock:
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.stdout = stdout
    cp.stderr = stderr
    cp.returncode = returncode
    return cp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOpenCodeClientSuccess:
    def test_run_returns_stdout(self, tmp_path: Path):
        client = _make_client(tmp_path)
        expected_output = '{"result": "success"}'
        with patch("subprocess.run", return_value=_make_completed_process(stdout=expected_output)):
            result = client.run(
                agent_name="test_agent",
                system_prompt="You are a test agent.",
                user_message="Hello",
            )
        assert result == expected_output

    def test_run_uses_correct_model(self, tmp_path: Path):
        client = _make_client(tmp_path)
        with patch("subprocess.run", return_value=_make_completed_process()) as mock_run:
            client.run(
                agent_name="test",
                system_prompt="sys",
                user_message="msg",
                model="my-special-model",
            )
        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "my-special-model"

    def test_run_uses_default_model_when_none(self, tmp_path: Path):
        client = _make_client(tmp_path)
        with patch("subprocess.run", return_value=_make_completed_process()) as mock_run:
            client.run(agent_name="t", system_prompt="s", user_message="m")
        cmd = mock_run.call_args[0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "test-model"

    def test_run_passes_file_flags(self, tmp_path: Path):
        client = _make_client(tmp_path)
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("a")
        f2.write_text("b")
        with patch("subprocess.run", return_value=_make_completed_process()) as mock_run:
            client.run(
                agent_name="t",
                system_prompt="s",
                user_message="m",
                file_paths=[f1, f2],
            )
        cmd = mock_run.call_args[0][0]
        assert "--file" in cmd
        file_values = [cmd[i + 1] for i, c in enumerate(cmd) if c == "--file"]
        assert str(f1) in file_values
        assert str(f2) in file_values

    def test_run_passes_timeout(self, tmp_path: Path):
        client = _make_client(tmp_path)
        with patch("subprocess.run", return_value=_make_completed_process()) as mock_run:
            client.run(agent_name="t", system_prompt="s", user_message="m", timeout=42)
        kwargs = mock_run.call_args[1]
        assert kwargs.get("timeout") == 42

    def test_no_error_log_on_success(self, tmp_path: Path):
        errors_dir = tmp_path / "errors"
        client = OpenCodeClient(error_log_dir=errors_dir)
        with patch("subprocess.run", return_value=_make_completed_process()):
            client.run(agent_name="ok_agent", system_prompt="s", user_message="m")
        # No error log file should be created
        if errors_dir.exists():
            assert not list(errors_dir.glob("*.log"))


class TestOpenCodeClientFailure:
    def test_non_zero_exit_raises_agent_error(self, tmp_path: Path):
        client = _make_client(tmp_path)
        with patch("subprocess.run", return_value=_make_completed_process(returncode=1, stderr="oops")):
            with pytest.raises(AgentError, match="failed"):
                client.run(agent_name="bad_agent", system_prompt="s", user_message="m")

    def test_non_zero_exit_writes_error_log(self, tmp_path: Path):
        errors_dir = tmp_path / "errors"
        client = OpenCodeClient(error_log_dir=errors_dir)
        with patch("subprocess.run", return_value=_make_completed_process(returncode=1, stderr="error msg")):
            with pytest.raises(AgentError):
                client.run(agent_name="failing_agent", system_prompt="s", user_message="m")
        log_file = errors_dir / "failing_agent.log"
        assert log_file.exists()
        assert "error msg" in log_file.read_text(encoding="utf-8")

    def test_timeout_raises_agent_error(self, tmp_path: Path):
        client = _make_client(tmp_path)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="opencode", timeout=5)):
            with pytest.raises(AgentError, match="timed out"):
                client.run(agent_name="slow_agent", system_prompt="s", user_message="m", timeout=5)

    def test_timeout_writes_error_log(self, tmp_path: Path):
        errors_dir = tmp_path / "errors"
        client = OpenCodeClient(error_log_dir=errors_dir)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="opencode", timeout=5)):
            with pytest.raises(AgentError):
                client.run(agent_name="slow_agent", system_prompt="s", user_message="m")
        log_file = errors_dir / "slow_agent.log"
        assert log_file.exists()

    def test_opencode_not_found_raises_agent_error(self, tmp_path: Path):
        client = _make_client(tmp_path)
        with patch("subprocess.run", side_effect=FileNotFoundError("opencode not found")):
            with pytest.raises(AgentError, match="not found on PATH"):
                client.run(agent_name="t", system_prompt="s", user_message="m")

    def test_no_error_log_dir_does_not_crash(self, tmp_path: Path):
        client = OpenCodeClient(error_log_dir=None)
        with patch("subprocess.run", return_value=_make_completed_process(returncode=1, stderr="err")):
            with pytest.raises(AgentError):
                client.run(agent_name="t", system_prompt="s", user_message="m")
        # Should not raise any additional exception


class TestAgentError:
    def test_agent_error_is_runtime_error(self):
        err = AgentError("something went wrong")
        assert isinstance(err, RuntimeError)
        assert str(err) == "something went wrong"
