"""
Unit tests for src/cli_client.py
"""

import json
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.cli_client import OpenCodeClient, AgentError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(tmp_path: Path) -> OpenCodeClient:
    return OpenCodeClient(error_log_dir=tmp_path / "errors", default_model="test-model")


class _FakeStderr:
    """Minimal stderr stand-in whose .read() returns a plain string."""
    def __init__(self, text: str = "") -> None:
        self._text = text

    def read(self) -> str:
        return self._text


class _FakePopen:
    """Minimal Popen stand-in that is safe to use in for-loops and with threading.Timer."""

    def __init__(
        self,
        stdout_lines: list[str] | None = None,
        returncode: int = 0,
        stderr_str: str = "",
    ) -> None:
        self.returncode = returncode
        self._lines = [(line + "\n") for line in (stdout_lines or [])]
        # stdout iterates over self directly
        self.stdout = self
        self.stderr = _FakeStderr(stderr_str)

    # Make `for line in proc.stdout` work (stdout == self)
    def __iter__(self):
        return iter(self._lines)

    def close(self) -> None:
        pass

    def wait(self) -> None:
        pass

    def kill(self) -> None:
        pass


def _make_popen_mock(
    stdout_lines: list[str] | None = None,
    returncode: int = 0,
    stderr: str = "",
) -> _FakePopen:
    """Return a _FakePopen that simulates a finished process."""
    return _FakePopen(stdout_lines=stdout_lines, returncode=returncode, stderr_str=stderr)


def _make_event_stream(text: str) -> list[str]:
    """Return stdout_lines list containing a minimal text event."""
    event = {"type": "text", "part": {"text": text}}
    return [json.dumps(event)]


def _make_instant_timer():
    """Return a threading.Timer factory that fires its callback immediately on .start()."""
    def _factory(interval, fn, *args, **kwargs):
        t = MagicMock()
        t.start = MagicMock(side_effect=fn)
        t.cancel = MagicMock()
        return t
    return _factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOpenCodeClientSuccess:
    def test_run_returns_stdout(self, tmp_path: Path):
        client = _make_client(tmp_path)
        expected_output = '{"result": "success"}'
        with patch("subprocess.Popen", return_value=_make_popen_mock(_make_event_stream(expected_output))):
            result = client.run(
                agent_name="test_agent",
                system_prompt="You are a test agent.",
                user_message="Hello",
            )
        assert result == expected_output

    def test_run_uses_correct_model(self, tmp_path: Path):
        client = _make_client(tmp_path)
        with patch("subprocess.Popen", return_value=_make_popen_mock()) as mock_popen:
            client.run(agent_name="test", system_prompt="sys", user_message="msg", model="my-special-model")
        cmd = mock_popen.call_args[0][0]
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "my-special-model"

    def test_run_uses_default_model_when_none(self, tmp_path: Path):
        client = _make_client(tmp_path)
        with patch("subprocess.Popen", return_value=_make_popen_mock()) as mock_popen:
            client.run(agent_name="t", system_prompt="s", user_message="m")
        cmd = mock_popen.call_args[0][0]
        assert cmd[cmd.index("--model") + 1] == "test-model"

    def test_run_omits_model_flag_when_no_model_configured(self, tmp_path: Path):
        client = OpenCodeClient(error_log_dir=tmp_path / "errors")  # default_model=None
        with patch("subprocess.Popen", return_value=_make_popen_mock()) as mock_popen:
            client.run(agent_name="t", system_prompt="s", user_message="m")
        cmd = mock_popen.call_args[0][0]
        assert "--model" not in cmd

    def test_run_passes_file_flags(self, tmp_path: Path):
        client = _make_client(tmp_path)
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("a")
        f2.write_text("b")
        with patch("subprocess.Popen", return_value=_make_popen_mock()) as mock_popen:
            client.run(agent_name="t", system_prompt="s", user_message="m", file_paths=[f1, f2])
        cmd = mock_popen.call_args[0][0]
        assert "--file" in cmd
        file_values = [cmd[i + 1] for i, c in enumerate(cmd) if c == "--file"]
        assert str(f1) in file_values
        assert str(f2) in file_values

    def test_run_passes_timeout_to_timer(self, tmp_path: Path):
        """timeout arg should be passed as the interval to threading.Timer."""
        client = _make_client(tmp_path)
        timer_calls: list = []

        def _fake_timer(interval, fn, *args, **kwargs):
            timer_calls.append(interval)
            t = MagicMock()
            t.start = MagicMock()
            t.cancel = MagicMock()
            return t

        with patch("subprocess.Popen", return_value=_make_popen_mock()):
            with patch("threading.Timer", side_effect=_fake_timer):
                client.run(agent_name="t", system_prompt="s", user_message="m", timeout=42)
        assert timer_calls == [42]

    def test_no_error_log_on_success(self, tmp_path: Path):
        errors_dir = tmp_path / "errors"
        client = OpenCodeClient(error_log_dir=errors_dir)
        with patch("subprocess.Popen", return_value=_make_popen_mock()):
            client.run(agent_name="ok_agent", system_prompt="s", user_message="m")
        if errors_dir.exists():
            assert not list(errors_dir.glob("*.log"))

    def test_on_event_callback_fired(self, tmp_path: Path):
        """on_event should be called for each parsed JSONL event."""
        client = _make_client(tmp_path)
        events_seen: list = []
        stream = _make_event_stream("hello world")
        with patch("subprocess.Popen", return_value=_make_popen_mock(stream)):
            client.run(
                agent_name="t", system_prompt="s", user_message="m",
                on_event=events_seen.append,
            )
        assert any(e.get("type") == "text" for e in events_seen)

    def test_on_event_callback_exception_does_not_crash(self, tmp_path: Path):
        """Exceptions raised in on_event must not propagate."""
        client = _make_client(tmp_path)
        def _bad_cb(e):
            raise RuntimeError("boom")
        stream = _make_event_stream("hi")
        with patch("subprocess.Popen", return_value=_make_popen_mock(stream)):
            result = client.run(
                agent_name="t", system_prompt="s", user_message="m",
                on_event=_bad_cb,
            )
        assert result == "hi"


class TestOpenCodeClientFailure:
    def test_non_zero_exit_raises_agent_error(self, tmp_path: Path):
        client = _make_client(tmp_path)
        with patch("subprocess.Popen", return_value=_make_popen_mock(returncode=1, stderr="oops")):
            with pytest.raises(AgentError, match="failed"):
                client.run(agent_name="bad_agent", system_prompt="s", user_message="m")

    def test_non_zero_exit_writes_error_log(self, tmp_path: Path):
        errors_dir = tmp_path / "errors"
        client = OpenCodeClient(error_log_dir=errors_dir)
        with patch("subprocess.Popen", return_value=_make_popen_mock(returncode=1, stderr="error msg")):
            with pytest.raises(AgentError):
                client.run(agent_name="failing_agent", system_prompt="s", user_message="m")
        log_file = errors_dir / "failing_agent.log"
        assert log_file.exists()
        assert "error msg" in log_file.read_text(encoding="utf-8")

    def test_timeout_raises_agent_error(self, tmp_path: Path):
        """When the timer fires, timed_out is set and AgentError is raised."""
        client = _make_client(tmp_path)
        with patch("subprocess.Popen", return_value=_make_popen_mock()):
            with patch("threading.Timer", side_effect=_make_instant_timer()):
                with pytest.raises(AgentError, match="timed out"):
                    client.run(agent_name="slow_agent", system_prompt="s", user_message="m", timeout=5)

    def test_timeout_writes_error_log(self, tmp_path: Path):
        errors_dir = tmp_path / "errors"
        client = OpenCodeClient(error_log_dir=errors_dir)
        with patch("subprocess.Popen", return_value=_make_popen_mock()):
            with patch("threading.Timer", side_effect=_make_instant_timer()):
                with pytest.raises(AgentError):
                    client.run(agent_name="slow_agent", system_prompt="s", user_message="m", timeout=1)
        log_file = errors_dir / "slow_agent.log"
        assert log_file.exists()

    def test_opencode_not_found_raises_agent_error(self, tmp_path: Path):
        client = _make_client(tmp_path)
        with patch("subprocess.Popen", side_effect=FileNotFoundError("opencode not found")):
            with pytest.raises(AgentError, match="not found on PATH"):
                client.run(agent_name="t", system_prompt="s", user_message="m")

    def test_no_error_log_dir_does_not_crash(self, tmp_path: Path):
        client = OpenCodeClient(error_log_dir=None)
        with patch("subprocess.Popen", return_value=_make_popen_mock(returncode=1, stderr="err")):
            with pytest.raises(AgentError):
                client.run(agent_name="t", system_prompt="s", user_message="m")


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

        def _fake_popen(cmd, **kwargs):
            # Agent file still exists here (before the finally block deletes it)
            for f in agents_dir.glob("mdt_*.md"):
                captured["content"] = f.read_text(encoding="utf-8")
            return _make_popen_mock()

        client = _make_client(tmp_path)
        with patch.object(OpenCodeClient, "AGENTS_DIR", agents_dir):
            with patch("subprocess.Popen", side_effect=_fake_popen):
                client.run(
                    agent_name="test_agent",
                    system_prompt="system",
                    user_message="hello",
                    **run_kwargs,
                )
        return captured.get("content", "")

    def test_default_read_permission_is_allow(self, tmp_path: Path):
        content = self._capture_frontmatter(tmp_path)
        assert "read: allow" in content

    def test_read_allowed_false_writes_deny(self, tmp_path: Path):
        content = self._capture_frontmatter(tmp_path, read_allowed=False)
        assert "read: deny" in content
        assert "read: allow" not in content

    def test_read_allowed_true_explicit_writes_allow(self, tmp_path: Path):
        content = self._capture_frontmatter(tmp_path, read_allowed=True)
        assert "read: allow" in content
        assert "read: deny" not in content

    def test_frontmatter_bash_denied_by_default(self, tmp_path: Path):
        content = self._capture_frontmatter(tmp_path)
        assert "bash: deny" in content
        assert "edit: deny" in content

    def test_bash_allowed_true_writes_allow(self, tmp_path: Path):
        content = self._capture_frontmatter(tmp_path, bash_allowed=True)
        assert "bash: allow" in content
        assert "edit: deny" in content

    def test_bash_allowed_false_writes_deny(self, tmp_path: Path):
        content = self._capture_frontmatter(tmp_path, bash_allowed=False)
        assert "bash: deny" in content

    def test_system_prompt_appended_after_frontmatter(self, tmp_path: Path):
        content = self._capture_frontmatter(tmp_path)
        assert "---" in content
        assert "system" in content

    def test_read_allowed_false_does_not_affect_subprocess_invocation(self, tmp_path: Path):
        agents_dir = tmp_path / ".opencode" / "agents"
        client = _make_client(tmp_path)
        with patch.object(OpenCodeClient, "AGENTS_DIR", agents_dir):
            with patch("subprocess.Popen", return_value=_make_popen_mock()) as mock_popen:
                client.run(agent_name="t", system_prompt="s", user_message="m", read_allowed=False)
        mock_popen.assert_called_once()


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

class TestAuditLogging:
    """Verify that audit JSON files are written to log_dir on success and failure."""

    def _make_client_with_logs(self, tmp_path: Path) -> tuple:
        log_dir = tmp_path / "logs"
        client = OpenCodeClient(
            error_log_dir=tmp_path / "errors",
            log_dir=log_dir,
            default_model="test-model",
        )
        return client, log_dir

    def test_audit_log_written_on_success(self, tmp_path: Path):
        client, log_dir = self._make_client_with_logs(tmp_path)
        agents_dir = tmp_path / ".opencode" / "agents"
        with patch.object(OpenCodeClient, "AGENTS_DIR", agents_dir):
            with patch("subprocess.Popen", return_value=_make_popen_mock()):
                client.run(agent_name="my_agent", system_prompt="s", user_message="u")
        logs = list(log_dir.glob("*_my_agent.json"))
        assert len(logs) == 1
        record = json.loads(logs[0].read_text())
        assert record["agent_name"] == "my_agent"
        assert record["exit_code"] == 0
        assert record["error"] is None
        assert record["duration_seconds"] >= 0
        assert record["model"] == "test-model"

    def test_audit_log_written_on_failure(self, tmp_path: Path):
        client, log_dir = self._make_client_with_logs(tmp_path)
        agents_dir = tmp_path / ".opencode" / "agents"
        with patch.object(OpenCodeClient, "AGENTS_DIR", agents_dir):
            with patch("subprocess.Popen", return_value=_make_popen_mock(returncode=1, stderr="boom")):
                with pytest.raises(AgentError):
                    client.run(agent_name="fail_agent", system_prompt="s", user_message="u")
        logs = list(log_dir.glob("*_fail_agent.json"))
        assert len(logs) == 1
        record = json.loads(logs[0].read_text())
        assert record["exit_code"] == 1
        assert record["error"] is not None
        assert record["stderr"] == "boom"

    def test_no_audit_log_when_log_dir_is_none(self, tmp_path: Path):
        client = OpenCodeClient(error_log_dir=tmp_path / "errors", log_dir=None)
        agents_dir = tmp_path / ".opencode" / "agents"
        with patch.object(OpenCodeClient, "AGENTS_DIR", agents_dir):
            with patch("subprocess.Popen", return_value=_make_popen_mock()):
                client.run(agent_name="x", system_prompt="s", user_message="u")
        assert not (tmp_path / "logs").exists()

    def test_audit_log_timeout_written(self, tmp_path: Path):
        client, log_dir = self._make_client_with_logs(tmp_path)
        agents_dir = tmp_path / ".opencode" / "agents"
        with patch.object(OpenCodeClient, "AGENTS_DIR", agents_dir):
            with patch("subprocess.Popen", return_value=_make_popen_mock()):
                with patch("threading.Timer", side_effect=_make_instant_timer()):
                    with pytest.raises(AgentError):
                        client.run(agent_name="slow_agent", system_prompt="s", user_message="u", timeout=5)
        logs = list(log_dir.glob("*_slow_agent.json"))
        assert len(logs) == 1
        record = json.loads(logs[0].read_text())
        assert "Timeout" in record["error"]
        assert record["exit_code"] is None

    def test_audit_log_contains_system_prompt_and_user_message(self, tmp_path: Path):
        """Audit log should record the actual system_prompt and user_message text."""
        client, log_dir = self._make_client_with_logs(tmp_path)
        agents_dir = tmp_path / ".opencode" / "agents"
        with patch.object(OpenCodeClient, "AGENTS_DIR", agents_dir):
            with patch("subprocess.Popen", return_value=_make_popen_mock()):
                client.run(
                    agent_name="audit_agent",
                    system_prompt="my system prompt",
                    user_message="my user message",
                )
        logs = list(log_dir.glob("*_audit_agent.json"))
        assert len(logs) == 1
        record = json.loads(logs[0].read_text())
        assert record["system_prompt"] == "my system prompt"
        assert record["user_message"] == "my user message"

    def test_audit_log_inputs_on_failure(self, tmp_path: Path):
        """Audit log on failure should also record system_prompt and user_message."""
        client, log_dir = self._make_client_with_logs(tmp_path)
        agents_dir = tmp_path / ".opencode" / "agents"
        with patch.object(OpenCodeClient, "AGENTS_DIR", agents_dir):
            with patch("subprocess.Popen", return_value=_make_popen_mock(returncode=1)):
                with pytest.raises(AgentError):
                    client.run(
                        agent_name="fail_audit_agent",
                        system_prompt="sys text",
                        user_message="user text",
                    )
        logs = list(log_dir.glob("*_fail_audit_agent.json"))
        assert len(logs) == 1
        record = json.loads(logs[0].read_text())
        assert record["system_prompt"] == "sys text"
        assert record["user_message"] == "user text"
