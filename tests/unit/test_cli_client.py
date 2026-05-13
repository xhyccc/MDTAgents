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


def _make_event_stream(text: str) -> str:
    """Return a minimal opencode --format json event stream containing *text*."""
    import json as _json
    event = {"type": "text", "part": {"text": text}}
    return _json.dumps(event) + "\n"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOpenCodeClientSuccess:
    def test_run_returns_stdout(self, tmp_path: Path):
        client = _make_client(tmp_path)
        expected_output = '{"result": "success"}'
        stream = _make_event_stream(expected_output)
        with patch("subprocess.run", return_value=_make_completed_process(stdout=stream)):
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

    def test_run_omits_model_flag_when_no_model_configured(self, tmp_path: Path):
        client = OpenCodeClient(error_log_dir=tmp_path / "errors")  # default_model=None
        with patch("subprocess.run", return_value=_make_completed_process()) as mock_run:
            client.run(agent_name="t", system_prompt="s", user_message="m")
        cmd = mock_run.call_args[0][0]
        assert "--model" not in cmd

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


# ---------------------------------------------------------------------------
# read_allowed permission flag
# ---------------------------------------------------------------------------

class TestReadAllowedPermission:
    """Verify that the `read_allowed` parameter controls the agent frontmatter."""

    def _capture_frontmatter(self, tmp_path: Path, **run_kwargs) -> str:
        """Run client.run() with a patched AGENTS_DIR; return the frontmatter written."""
        agents_dir = tmp_path / ".opencode" / "agents"
        captured: dict = {}

        def _fake_run(cmd, **kwargs):
            # Agent file exists here — capture before finally block deletes it.
            for f in agents_dir.glob("mdt_*.md"):
                captured["content"] = f.read_text(encoding="utf-8")
            return _make_completed_process()

        client = _make_client(tmp_path)
        with patch.object(OpenCodeClient, "AGENTS_DIR", agents_dir):
            with patch("subprocess.run", side_effect=_fake_run):
                client.run(
                    agent_name="test_agent",
                    system_prompt="system",
                    user_message="hello",
                    **run_kwargs,
                )
        return captured.get("content", "")

    def test_default_read_permission_is_allow(self, tmp_path: Path):
        """No read_allowed kwarg → frontmatter has 'read: allow'."""
        content = self._capture_frontmatter(tmp_path)
        assert "read: allow" in content

    def test_read_allowed_false_writes_deny(self, tmp_path: Path):
        """read_allowed=False → frontmatter has 'read: deny'."""
        content = self._capture_frontmatter(tmp_path, read_allowed=False)
        assert "read: deny" in content
        assert "read: allow" not in content

    def test_read_allowed_true_explicit_writes_allow(self, tmp_path: Path):
        """read_allowed=True (explicit) → frontmatter has 'read: allow'."""
        content = self._capture_frontmatter(tmp_path, read_allowed=True)
        assert "read: allow" in content
        assert "read: deny" not in content

    def test_frontmatter_bash_always_denied(self, tmp_path: Path):
        """bash and edit permissions should always be deny regardless of read_allowed."""
        for flag in (True, False):
            content = self._capture_frontmatter(tmp_path, read_allowed=flag)
            assert "bash: deny" in content
            assert "edit: deny" in content

    def test_system_prompt_appended_after_frontmatter(self, tmp_path: Path):
        """System prompt text should follow the YAML frontmatter block."""
        content = self._capture_frontmatter(tmp_path)
        # The frontmatter ends with '---', then system prompt follows
        assert "---" in content
        assert "system" in content  # matches system_prompt="system"

    def test_read_allowed_false_does_not_affect_subprocess_invocation(self, tmp_path: Path):
        """read_allowed=False should not suppress the subprocess call itself."""
        agents_dir = tmp_path / ".opencode" / "agents"
        client = _make_client(tmp_path)
        with patch.object(OpenCodeClient, "AGENTS_DIR", agents_dir):
            with patch("subprocess.run", return_value=_make_completed_process()) as mock_run:
                client.run(
                    agent_name="t",
                    system_prompt="s",
                    user_message="m",
                    read_allowed=False,
                )
        mock_run.assert_called_once()
