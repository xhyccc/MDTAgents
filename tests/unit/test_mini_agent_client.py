"""
Unit tests for MiniAgentClient and make_agent_client() in src/cli_client.py
"""

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.cli_client import AgentError, MiniAgentClient, OpenCodeClient, make_agent_client


# ---------------------------------------------------------------------------
# Helpers shared with existing test_cli_client.py conventions
# ---------------------------------------------------------------------------

class _FakeStderr:
    def __init__(self, text: str = "") -> None:
        self._text = text

    def read(self) -> str:
        return self._text


class _FakePopen:
    def __init__(self, stdout_lines=None, returncode: int = 0, stderr_str: str = "") -> None:
        self.returncode = returncode
        self._lines = [(line + "\n") for line in (stdout_lines or [])]
        self.stdout = self
        self.stderr = _FakeStderr(stderr_str)

    def __iter__(self):
        return iter(self._lines)

    def close(self) -> None:
        pass

    def wait(self) -> None:
        pass

    def kill(self) -> None:
        pass


def _make_mini_client(tmp_path: Path, **kwargs) -> MiniAgentClient:
    defaults = dict(
        api_key="sk-test",
        base_url="https://api.moonshot.cn/v1",
        mini_agent_cmd="mini-coding-agent",
        max_steps=10,
        error_log_dir=tmp_path / "errors",
        default_model="moonshot-v1-128k",
        log_dir=tmp_path / "logs",
    )
    defaults.update(kwargs)
    return MiniAgentClient(**defaults)


# ---------------------------------------------------------------------------
# MiniAgentClient._extract_text
# ---------------------------------------------------------------------------

class TestMiniAgentExtractText:
    def test_final_tag_extracted(self, tmp_path):
        client = _make_mini_client(tmp_path)
        stdout = "Some step output...\n<final>This is the answer.</final>\n"
        assert client._extract_text(stdout) == "This is the answer."

    def test_last_final_tag_wins(self, tmp_path):
        client = _make_mini_client(tmp_path)
        stdout = "<final>first</final>\nmore stuff\n<final>second</final>"
        assert client._extract_text(stdout) == "second"

    def test_case_insensitive_final_tag(self, tmp_path):
        client = _make_mini_client(tmp_path)
        stdout = "<FINAL>Answer here</FINAL>"
        assert client._extract_text(stdout) == "Answer here"

    def test_multiline_final_tag(self, tmp_path):
        client = _make_mini_client(tmp_path)
        stdout = "<final>\nLine one\nLine two\n</final>"
        assert "Line one" in client._extract_text(stdout)
        assert "Line two" in client._extract_text(stdout)

    def test_no_final_tag_returns_all_stdout(self, tmp_path):
        client = _make_mini_client(tmp_path)
        stdout = "Plain text output with no final tag."
        assert client._extract_text(stdout) == "Plain text output with no final tag."

    def test_empty_stdout_returns_empty(self, tmp_path):
        client = _make_mini_client(tmp_path)
        assert client._extract_text("") == ""

    def test_whitespace_only_stdout_returns_empty(self, tmp_path):
        client = _make_mini_client(tmp_path)
        assert client._extract_text("   \n  \n") == ""


# ---------------------------------------------------------------------------
# MiniAgentClient.run — success paths
# ---------------------------------------------------------------------------

class TestMiniAgentClientSuccess:
    def test_run_returns_final_tag_content(self, tmp_path):
        client = _make_mini_client(tmp_path)
        stdout_lines = ["Step 1 done.", "<final>Great analysis here.</final>"]
        with patch("subprocess.Popen", return_value=_FakePopen(stdout_lines)):
            result = client.run(
                agent_name="test", system_prompt="sys", user_message="user"
            )
        assert result == "Great analysis here."

    def test_run_falls_back_to_full_stdout(self, tmp_path):
        client = _make_mini_client(tmp_path)
        stdout_lines = ["Plain answer without tags."]
        with patch("subprocess.Popen", return_value=_FakePopen(stdout_lines)):
            result = client.run(
                agent_name="test", system_prompt="sys", user_message="user"
            )
        assert result == "Plain answer without tags."

    def test_run_builds_openai_backend_cmd(self, tmp_path):
        client = _make_mini_client(tmp_path)
        with patch("subprocess.Popen", return_value=_FakePopen()) as mock_popen:
            client.run(agent_name="t", system_prompt="s", user_message="m")
        cmd = mock_popen.call_args[0][0]
        assert "--backend" in cmd
        assert cmd[cmd.index("--backend") + 1] == "openai"
        assert "--openai-api-key" in cmd
        assert cmd[cmd.index("--openai-api-key") + 1] == "sk-test"
        assert "--openai-base-url" in cmd
        assert cmd[cmd.index("--openai-base-url") + 1] == "https://api.moonshot.cn/v1"

    def test_run_uses_approval_auto(self, tmp_path):
        client = _make_mini_client(tmp_path)
        with patch("subprocess.Popen", return_value=_FakePopen()) as mock_popen:
            client.run(agent_name="t", system_prompt="s", user_message="m")
        cmd = mock_popen.call_args[0][0]
        assert "--approval" in cmd
        assert cmd[cmd.index("--approval") + 1] == "auto"

    def test_run_uses_correct_model(self, tmp_path):
        client = _make_mini_client(tmp_path)
        with patch("subprocess.Popen", return_value=_FakePopen()) as mock_popen:
            client.run(agent_name="t", system_prompt="s", user_message="m", model="glm-4")
        cmd = mock_popen.call_args[0][0]
        assert cmd[cmd.index("--model") + 1] == "glm-4"

    def test_run_uses_default_model_fallback(self, tmp_path):
        client = _make_mini_client(tmp_path, default_model="moonshot-v1-128k")
        with patch("subprocess.Popen", return_value=_FakePopen()) as mock_popen:
            client.run(agent_name="t", system_prompt="s", user_message="m")
        cmd = mock_popen.call_args[0][0]
        assert cmd[cmd.index("--model") + 1] == "moonshot-v1-128k"

    def test_run_allow_read_only_by_default(self, tmp_path):
        client = _make_mini_client(tmp_path)
        with patch("subprocess.Popen", return_value=_FakePopen()) as mock_popen:
            client.run(agent_name="t", system_prompt="s", user_message="m",
                       read_allowed=True, bash_allowed=False)
        cmd = mock_popen.call_args[0][0]
        allow_idx = cmd.index("--allow")
        # Collect all non-flag args immediately after --allow
        allow_vals = []
        for item in cmd[allow_idx + 1:]:
            if item.startswith("--"):
                break
            allow_vals.append(item)
        assert "read" in allow_vals
        assert "bash" not in allow_vals

    def test_run_allow_includes_bash_when_requested(self, tmp_path):
        client = _make_mini_client(tmp_path)
        with patch("subprocess.Popen", return_value=_FakePopen()) as mock_popen:
            client.run(agent_name="t", system_prompt="s", user_message="m",
                       bash_allowed=True)
        cmd = mock_popen.call_args[0][0]
        allow_idx = cmd.index("--allow")
        allow_vals = []
        for item in cmd[allow_idx + 1:]:
            if item.startswith("--"):
                break
            allow_vals.append(item)
        assert "bash" in allow_vals

    def test_run_openai_timeout_in_cmd(self, tmp_path):
        """--openai-timeout must appear in the command."""
        client = _make_mini_client(tmp_path, openai_timeout=600)
        with patch("subprocess.Popen", return_value=_FakePopen()) as mock_popen:
            client.run(agent_name="t", system_prompt="s", user_message="m")
        cmd = mock_popen.call_args[0][0]
        assert "--openai-timeout" in cmd
        assert cmd[cmd.index("--openai-timeout") + 1] == "600"

    def test_run_max_new_tokens_in_cmd(self, tmp_path):
        """--max-new-tokens must appear in the command."""
        client = _make_mini_client(tmp_path, max_new_tokens=4096)
        with patch("subprocess.Popen", return_value=_FakePopen()) as mock_popen:
            client.run(agent_name="t", system_prompt="s", user_message="m")
        cmd = mock_popen.call_args[0][0]
        assert "--max-new-tokens" in cmd
        assert cmd[cmd.index("--max-new-tokens") + 1] == "4096"

    def test_run_uses_start_new_session(self, tmp_path):
        """Subprocess must use start_new_session=True to isolate from SIGINT."""
        client = _make_mini_client(tmp_path)
        with patch("subprocess.Popen", return_value=_FakePopen()) as mock_popen:
            client.run(agent_name="t", system_prompt="s", user_message="m")
        kwargs = mock_popen.call_args[1]
        assert kwargs.get("start_new_session") is True

    def test_run_inlines_files_when_read_not_allowed(self, tmp_path):
        """When read_allowed=False, file content must be embedded in the task."""
        client = _make_mini_client(tmp_path)
        f = tmp_path / "report.md"
        f.write_text("# Medical Report\nContent here.")
        with patch("subprocess.Popen", return_value=_FakePopen()) as mock_popen:
            client.run(agent_name="t", system_prompt="s", user_message="u",
                       file_paths=[f], read_allowed=False)
        cmd = mock_popen.call_args[0][0]
        task_arg = cmd[-1]  # last positional argument is the task
        assert "Medical Report" in task_arg
        assert "Content here" in task_arg

    def test_run_on_event_callback_fired(self, tmp_path):
        client = _make_mini_client(tmp_path)
        events: list = []
        with patch("subprocess.Popen", return_value=_FakePopen(["output line"])):
            client.run(agent_name="t", system_prompt="s", user_message="m",
                       on_event=events.append)
        assert len(events) >= 1
        assert all(e.get("type") == "text" for e in events)

    def test_run_on_event_exception_does_not_crash(self, tmp_path):
        client = _make_mini_client(tmp_path)
        def _bad(e):
            raise RuntimeError("boom")
        with patch("subprocess.Popen", return_value=_FakePopen(["<final>ok</final>"])):
            result = client.run(agent_name="t", system_prompt="s", user_message="m",
                                on_event=_bad)
        assert result == "ok"

    def test_run_writes_audit_log(self, tmp_path):
        client = _make_mini_client(tmp_path, log_dir=tmp_path / "logs")
        with patch("subprocess.Popen", return_value=_FakePopen(["<final>done</final>"])):
            client.run(agent_name="myagent", system_prompt="s", user_message="m")
        logs = list((tmp_path / "logs").glob("*.json"))
        assert len(logs) == 1
        record = json.loads(logs[0].read_text())
        assert record["agent_name"] == "myagent"
        assert record["backend"] == "mini_agent"
        assert record["error"] is None

    def test_run_prepends_system_prompt_to_task(self, tmp_path):
        client = _make_mini_client(tmp_path)
        with patch("subprocess.Popen", return_value=_FakePopen()) as mock_popen:
            client.run(agent_name="t", system_prompt="Be a doctor.", user_message="Diagnose X.")
        task_arg = mock_popen.call_args[0][0][-1]
        assert "Be a doctor." in task_arg
        assert "Diagnose X." in task_arg


# ---------------------------------------------------------------------------
# MiniAgentClient.run — failure paths
# ---------------------------------------------------------------------------

class TestMiniAgentClientFailure:
    def test_non_zero_exit_raises_agent_error(self, tmp_path):
        client = _make_mini_client(tmp_path)
        with patch("subprocess.Popen", return_value=_FakePopen(returncode=1)):
            with pytest.raises(AgentError, match="failed"):
                client.run(agent_name="bad", system_prompt="s", user_message="m")

    def test_non_zero_exit_writes_error_log(self, tmp_path):
        client = _make_mini_client(tmp_path, error_log_dir=tmp_path / "errors")
        with patch("subprocess.Popen", return_value=_FakePopen(returncode=1, stderr_str="oops")):
            with pytest.raises(AgentError):
                client.run(agent_name="bad_agent", system_prompt="s", user_message="m")
        log = tmp_path / "errors" / "bad_agent.log"
        assert log.exists()

    def test_file_not_found_raises_agent_error(self, tmp_path):
        client = _make_mini_client(tmp_path, mini_agent_cmd="nonexistent-cmd-xyz")
        with patch("subprocess.Popen", side_effect=FileNotFoundError("not found")):
            with pytest.raises(AgentError, match="not found"):
                client.run(agent_name="t", system_prompt="s", user_message="m")

    def test_timeout_raises_agent_error(self, tmp_path):
        """Simulate timer firing immediately."""
        client = _make_mini_client(tmp_path)

        def _instant_timer(interval, fn, *args, **kwargs):
            t = MagicMock()
            t.start = MagicMock(side_effect=fn)
            t.cancel = MagicMock()
            return t

        with patch("subprocess.Popen", return_value=_FakePopen()):
            with patch("threading.Timer", side_effect=_instant_timer):
                with pytest.raises(AgentError, match="timed out"):
                    client.run(agent_name="slow", system_prompt="s", user_message="m",
                               timeout=1)

    def test_timeout_writes_audit_log_with_none_exit_code(self, tmp_path):
        client = _make_mini_client(tmp_path, log_dir=tmp_path / "logs")

        def _instant_timer(interval, fn, *args, **kwargs):
            t = MagicMock()
            t.start = MagicMock(side_effect=fn)
            t.cancel = MagicMock()
            return t

        with patch("subprocess.Popen", return_value=_FakePopen()):
            with patch("threading.Timer", side_effect=_instant_timer):
                with pytest.raises(AgentError):
                    client.run(agent_name="slow_log", system_prompt="s", user_message="m")
        logs = list((tmp_path / "logs").glob("*.json"))
        assert logs
        record = json.loads(logs[0].read_text())
        assert record["exit_code"] is None


# ---------------------------------------------------------------------------
# make_agent_client
# ---------------------------------------------------------------------------

class TestMakeAgentClient:
    # Each test that calls make_agent_client() without an explicit env_file must
    # prevent auto-discovery of the real project .env by pointing MDT_ENV_FILE
    # at a nonexistent path.  We also clear all LLM_* vars that take priority
    # over provider-specific keys in load_env() and can leak into the test
    # process when the real .env has been sourced by the shell.
    def _block_dotenv(self, monkeypatch) -> None:
        monkeypatch.setenv("MDT_ENV_FILE", "/nonexistent/.env")
        for var in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_PROVIDER"):
            monkeypatch.delenv(var, raising=False)

    def test_returns_opencode_client_by_default(self, tmp_path, monkeypatch):
        self._block_dotenv(monkeypatch)
        monkeypatch.delenv("AGENT_BACKEND", raising=False)
        cfg = {}  # no backend key
        client = make_agent_client(cfg, error_log_dir=tmp_path / "e", log_dir=tmp_path / "l")
        assert isinstance(client, OpenCodeClient)

    def test_returns_opencode_when_cfg_says_opencode(self, tmp_path, monkeypatch):
        self._block_dotenv(monkeypatch)
        monkeypatch.delenv("AGENT_BACKEND", raising=False)
        cfg = {"backend": "opencode"}
        client = make_agent_client(cfg)
        assert isinstance(client, OpenCodeClient)

    def test_returns_mini_agent_when_cfg_says_mini_agent(self, tmp_path, monkeypatch):
        self._block_dotenv(monkeypatch)
        monkeypatch.delenv("AGENT_BACKEND", raising=False)
        monkeypatch.setenv("KIMI_API_KEY", "sk-test")
        cfg = {"backend": "mini_agent"}
        client = make_agent_client(cfg)
        assert isinstance(client, MiniAgentClient)

    def test_env_agent_backend_selects_mini_agent(self, tmp_path, monkeypatch):
        self._block_dotenv(monkeypatch)
        monkeypatch.setenv("AGENT_BACKEND", "mini_agent")
        monkeypatch.setenv("KIMI_API_KEY", "sk-env-key")
        cfg = {}  # no yaml backend override
        client = make_agent_client(cfg)
        assert isinstance(client, MiniAgentClient)

    def test_cfg_backend_overrides_env_backend(self, tmp_path, monkeypatch):
        """config/system.yaml backend key takes priority over AGENT_BACKEND env var."""
        self._block_dotenv(monkeypatch)
        monkeypatch.setenv("AGENT_BACKEND", "mini_agent")
        monkeypatch.setenv("KIMI_API_KEY", "sk-x")
        cfg = {"backend": "opencode"}
        client = make_agent_client(cfg)
        assert isinstance(client, OpenCodeClient)

    def test_mini_agent_without_api_key_raises(self, tmp_path, monkeypatch):
        """mini_agent backend without an API key must raise AgentError at creation time."""
        self._block_dotenv(monkeypatch)
        monkeypatch.delenv("AGENT_BACKEND", raising=False)
        for k in ("KIMI_API_KEY", "MOONSHOT_API_KEY", "ZHIPU_API_KEY",
                  "SILICONFLOW_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        cfg = {"backend": "mini_agent"}
        with pytest.raises(AgentError, match="API key"):
            make_agent_client(cfg)

    def test_mini_agent_receives_correct_api_key(self, tmp_path, monkeypatch):
        self._block_dotenv(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "kimi")
        monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-factory")
        cfg = {"backend": "mini_agent"}
        client = make_agent_client(cfg)
        assert isinstance(client, MiniAgentClient)
        assert client.api_key == "sk-kimi-factory"

    def test_default_model_from_cfg_overrides_env(self, tmp_path, monkeypatch):
        """default_model in yaml cfg should take priority over LLM_MODEL in .env."""
        self._block_dotenv(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "kimi")
        monkeypatch.setenv("KIMI_API_KEY", "sk-x")
        monkeypatch.setenv("LLM_MODEL", "moonshot-v1-32k")
        cfg = {"backend": "mini_agent", "default_model": "moonshot-v1-128k"}
        client = make_agent_client(cfg)
        assert isinstance(client, MiniAgentClient)
        assert client.default_model == "moonshot-v1-128k"

    def test_error_and_log_dirs_forwarded(self, tmp_path, monkeypatch):
        self._block_dotenv(monkeypatch)
        monkeypatch.delenv("AGENT_BACKEND", raising=False)
        cfg = {}
        err_dir = tmp_path / "errors"
        log_dir = tmp_path / "logs"
        client = make_agent_client(cfg, error_log_dir=err_dir, log_dir=log_dir)
        assert isinstance(client, OpenCodeClient)
        assert client.error_log_dir == err_dir
        assert client.log_dir == log_dir

    def test_make_agent_client_reads_env_file(self, tmp_path, monkeypatch):
        """env_file argument should be loaded and applied."""
        for k in ("AGENT_BACKEND", "LLM_PROVIDER", "KIMI_API_KEY", "LLM_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(
            "AGENT_BACKEND=mini_agent\n"
            "LLM_PROVIDER=kimi\n"
            "KIMI_API_KEY=sk-from-file\n"
        )
        cfg = {}
        client = make_agent_client(cfg, env_file=env_file)
        assert isinstance(client, MiniAgentClient)
        assert client.api_key == "sk-from-file"
