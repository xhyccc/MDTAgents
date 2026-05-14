"""
cli_client.py — Agent CLI wrappers and backend factory.

Two concrete clients are provided:

* ``OpenCodeClient``  — wraps the opencode v1.4.3+ CLI (default).
* ``MiniAgentClient`` — wraps mini-coding-agent-CLI in one-shot mode,
  using any OpenAI-compatible API (Kimi, Moonshot, Zhipu, SiliconFlow …).

Use ``make_agent_client()`` to select the active backend from configuration
and .env, rather than constructing clients directly.

All Agent invocations go through this module.  Errors are written to
.mdt_workspace/errors/{agent_name}.log and surfaced as AgentError.

Full audit traces (request + response + timing) are written to
.mdt_workspace/logs/{timestamp}_{agent_name}.json for debugging.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Union

if TYPE_CHECKING:
    from src.env_config import LLMConfig


class AgentError(RuntimeError):
    """Raised when an agent CLI call fails."""


class OpenCodeClient:
    """
    Wraps ``opencode run`` CLI invocations (opencode v1.4.3+).

    Each call writes a temporary agent markdown file into ``.opencode/agents/``
    so the system prompt is applied correctly, then invokes
    ``opencode run --agent <name> --format json`` and parses the JSON event
    stream to extract the assistant text.
    """

    AGENTS_DIR = Path(".opencode") / "agents"

    def __init__(
        self,
        error_log_dir: Optional[Path] = None,
        default_model: Optional[str] = None,
        log_dir: Optional[Path] = None,
    ):
        self.error_log_dir = error_log_dir
        self.default_model = default_model
        self.log_dir = log_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        agent_name: str,
        system_prompt: str,
        user_message: str,
        file_paths: Optional[List[Path]] = None,
        model: Optional[str] = None,
        timeout: int = 300,
        read_allowed: bool = True,
        bash_allowed: bool = False,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> str:
        """
        Call ``opencode run`` with the supplied parameters.

        The subprocess stdout is streamed line-by-line. Each parsed JSONL event
        is passed to *on_event* (if provided) as it arrives, enabling real-time
        progress callbacks.  The full raw stdout is also written to a live JSONL
        file in *log_dir* during execution, replaced by the final audit JSON on
        completion.

        Parameters
        ----------
        agent_name:
            Logical name used in error-log/audit filenames.
        system_prompt:
            Full system-prompt text; embedded in a temporary agent file.
        user_message:
            The user turn message sent to the model.
        file_paths:
            Optional list of files to attach (``--file`` flags).
        model:
            Model override; falls back to *self.default_model*.
        timeout:
            Maximum seconds to wait for the subprocess.
        read_allowed:
            When False, the agent's ``read`` tool permission is set to ``deny``.
        bash_allowed:
            When True, the agent's ``bash`` tool permission is set to ``allow``.
            Defaults to False.  Enable for specialists and synthesis so the
            model can shell out within the agent workspace if needed.
        on_event:
            Optional callback called with each parsed JSONL event dict as it
            streams.  Called from the thread that invokes ``run()``.  Do not
            raise inside this callback — exceptions are silently ignored.

        Returns
        -------
        str
            The assistant's text response extracted from the JSON event stream.

        Raises
        ------
        AgentError
            If the process exits with a non-zero code or times out.
        """
        effective_model = model or self.default_model

        # Write a temporary agent markdown file with the system prompt.
        # Agent files placed in .opencode/agents/ are auto-discovered by opencode.
        self.AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        temp_agent_name = f"mdt_{agent_name}_{uuid.uuid4().hex[:8]}"
        agent_path = self.AGENTS_DIR / f"{temp_agent_name}.md"

        read_perm = "allow" if read_allowed else "deny"
        bash_perm = "allow" if bash_allowed else "deny"
        frontmatter = (
            "---\n"
            "description: Temporary MDT agent\n"
            "mode: primary\n"
            "permission:\n"
            f"  bash: {bash_perm}\n"
            "  edit: deny\n"
            f"  read: {read_perm}\n"
            "---\n\n"
        )
        agent_path.write_text(frontmatter + system_prompt, encoding="utf-8")

        cmd = [
            "opencode",
            "run",
            "--agent", temp_agent_name,
            "--dangerously-skip-permissions",
            "--format", "json",
        ]
        if effective_model:
            cmd += ["--model", effective_model]
        for fp in (file_paths or []):
            cmd += ["--file", str(fp)]
        # Use -- to explicitly separate flags from the positional message argument.
        # Without this, yargs' --file [array] greedily consumes the message text.
        cmd += ["--", user_message]

        started_at = datetime.now(timezone.utc).isoformat()
        start_time = time.monotonic()

        # Live JSONL log: events are appended as they stream, then replaced by
        # the final audit JSON when the call completes successfully.
        live_log_path: Optional[Path] = None
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            ts = started_at[:19].replace(":", "-")
            live_log_path = self.log_dir / f"{ts}_{agent_name}.live.jsonl"

        stdout_lines: List[str] = []
        stderr_chunks: List[str] = []
        timed_out = False
        returncode: Optional[int] = None

        try:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    # Isolate opencode in its own session so that SIGINT sent
                    # to the Streamlit terminal (Ctrl-C, app restart, etc.)
                    # does NOT propagate to the child process.
                    # The timer kill uses proc.kill() (SIGKILL by PID) so it
                    # still works correctly with start_new_session=True.
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                duration = time.monotonic() - start_time
                self._write_error(agent_name, f"opencode CLI not found: {exc}")
                self._write_audit_log(
                    agent_name=agent_name, model=effective_model,
                    started_at=started_at, duration_seconds=duration,
                    timeout=timeout, user_message_length=len(user_message),
                    system_prompt_length=len(system_prompt), exit_code=None,
                    stdout="", stderr=str(exc), response_text_length=0,
                    error=f"FileNotFoundError: {exc}",
                    system_prompt=system_prompt, user_message=user_message,
                )
                raise AgentError(
                    "opencode CLI not found on PATH. Please install it first."
                ) from exc

            # Drain stderr in a background thread to prevent pipe deadlock.
            def _drain_stderr() -> None:
                stderr_chunks.append(proc.stderr.read())

            stderr_th = threading.Thread(target=_drain_stderr, daemon=True)
            stderr_th.start()

            # Kill the process after *timeout* seconds if it hasn't finished.
            def _kill_on_timeout() -> None:
                nonlocal timed_out
                timed_out = True
                proc.kill()

            timer = threading.Timer(timeout, _kill_on_timeout)
            timer.start()

            live_log = None
            if live_log_path:
                live_log = live_log_path.open("w", encoding="utf-8", buffering=1)

            try:
                for raw_line in proc.stdout:
                    line = raw_line.rstrip("\n")
                    stdout_lines.append(line)
                    if live_log:
                        live_log.write(line + "\n")
                    if line:
                        try:
                            event = json.loads(line)
                            if on_event:
                                try:
                                    on_event(event)
                                except Exception:  # noqa: BLE001
                                    pass  # never let a callback crash the client
                        except json.JSONDecodeError:
                            pass
            finally:
                if live_log:
                    live_log.close()

            proc.stdout.close()
            proc.wait()
            timer.cancel()
            stderr_th.join(timeout=5)
            returncode = proc.returncode

        finally:
            agent_path.unlink(missing_ok=True)

        duration = time.monotonic() - start_time
        stdout = "\n".join(stdout_lines)
        stderr_text = stderr_chunks[0] if stderr_chunks else ""

        if timed_out:
            self._write_error(agent_name, f"Timeout after {timeout}s")
            self._write_audit_log(
                agent_name=agent_name, model=effective_model,
                started_at=started_at, duration_seconds=duration,
                timeout=timeout, user_message_length=len(user_message),
                system_prompt_length=len(system_prompt), exit_code=None,
                stdout=stdout, stderr=stderr_text, response_text_length=0,
                error=f"TimeoutExpired after {timeout}s",
                system_prompt=system_prompt, user_message=user_message,
            )
            if live_log_path and live_log_path.exists():
                live_log_path.unlink(missing_ok=True)
            raise AgentError(f"Agent '{agent_name}' timed out after {timeout}s")

        if returncode != 0:
            self._write_error(agent_name, stderr_text)
            self._write_audit_log(
                agent_name=agent_name, model=effective_model,
                started_at=started_at, duration_seconds=duration,
                timeout=timeout, user_message_length=len(user_message),
                system_prompt_length=len(system_prompt), exit_code=returncode,
                stdout=stdout, stderr=stderr_text, response_text_length=0,
                error=f"exit code {returncode}",
                system_prompt=system_prompt, user_message=user_message,
            )
            if live_log_path and live_log_path.exists():
                live_log_path.unlink(missing_ok=True)
            raise AgentError(
                f"Agent '{agent_name}' failed (exit {returncode}). "
                "See error log for details."
            )

        response_text = self._extract_text(stdout)
        self._write_audit_log(
            agent_name=agent_name, model=effective_model,
            started_at=started_at, duration_seconds=duration,
            timeout=timeout, user_message_length=len(user_message),
            system_prompt_length=len(system_prompt), exit_code=returncode,
            stdout=stdout, stderr=stderr_text,
            response_text_length=len(response_text), error=None,
            system_prompt=system_prompt, user_message=user_message,
        )
        # Live log is superseded by the final audit JSON; remove it.
        if live_log_path and live_log_path.exists():
            live_log_path.unlink(missing_ok=True)
        return response_text

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_text(self, json_output: str) -> str:
        """Parse the opencode --format json event stream and return assistant text."""
        texts: List[str] = []
        for line in json_output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if event.get("type") == "text":
                    part = event.get("part", {})
                    text = part.get("text", "")
                    if text:
                        texts.append(text)
            except json.JSONDecodeError:
                pass
        return "".join(texts)

    def _write_error(self, agent_name: str, message: str) -> None:
        if self.error_log_dir is None:
            return
        self.error_log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.error_log_dir / f"{agent_name}.log"
        try:
            log_path.write_text(message, encoding="utf-8")
        except OSError:
            pass

    def _write_audit_log(
        self,
        agent_name: str,
        model: Optional[str],
        started_at: str,
        duration_seconds: float,
        timeout: int,
        user_message_length: int,
        system_prompt_length: int,
        exit_code: Optional[int],
        stdout: str,
        stderr: str,
        response_text_length: int,
        error: Optional[str],
        system_prompt: str = "",
        user_message: str = "",
    ) -> None:
        """Write a full audit JSON to logs_dir/{timestamp}_{agent_name}.json."""
        if self.log_dir is None:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # Use the ISO timestamp prefix, replacing colons/dots for filesystem safety
        ts = started_at[:19].replace(":", "-").replace(" ", "T")
        filename = f"{ts}_{agent_name}.json"
        record = {
            "agent_name": agent_name,
            "model": model,
            "started_at": started_at,
            "duration_seconds": round(duration_seconds, 3),
            "timeout": timeout,
            "user_message_length": user_message_length,
            "system_prompt_length": system_prompt_length,
            "exit_code": exit_code,
            "stdout_size": len(stdout),
            "response_text_length": response_text_length,
            "error": error,
            "system_prompt": system_prompt,
            "user_message": user_message,
            "stdout": stdout,
            "stderr": stderr,
        }
        try:
            (self.log_dir / filename).write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass


# ---------------------------------------------------------------------------
# MiniAgentClient — wraps mini-coding-agent-CLI
# ---------------------------------------------------------------------------

#: Regex to extract the last ``<final>…</final>`` block from mini-agent output.
_FINAL_TAG_RE = re.compile(r"<final>(.*?)</final>", re.DOTALL | re.IGNORECASE)


def _strip_mini_agent_banner(text: str) -> str:
    """Remove the mini-coding-agent ASCII banner from the start of *text*.

    The banner is a contiguous block of lines at the top of stdout where every
    line begins with ``+`` or ``|`` (box-drawing characters).  We drop those
    lines plus any blank lines that follow.
    """
    lines = text.split("\n")
    i = 0
    while i < len(lines) and lines[i].startswith(("+", "|")):
        i += 1
    # Skip blank lines between banner and payload
    while i < len(lines) and not lines[i].strip():
        i += 1
    return "\n".join(lines[i:])


class MiniAgentClient:
    """
    Calls mini-coding-agent-CLI (https://github.com/xhyccc/mini-coding-agent-CLI)
    in **one-shot mode** as a drop-in replacement for :class:`OpenCodeClient`.

    The CLI is invoked as::

        mini-coding-agent --backend openai \\
            --openai-api-key KEY --openai-base-url URL \\
            --model MODEL --approval auto \\
            --allow read[,bash] --max-steps N --cwd CWD \\
            "TASK"

    ``system_prompt`` is prepended to ``user_message`` to form the task string.
    File contents are inlined when ``read_allowed=False``; otherwise the agent
    may read them through its ``read_file`` tool.

    Response extraction: looks for ``<final>…</final>`` in stdout first; falls
    back to the entire stdout if the tag is absent.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        mini_agent_cmd: str = "mini-coding-agent",
        max_steps: int = 15,
        openai_timeout: int = 300,
        max_new_tokens: int = 8192,
        error_log_dir: Optional[Path] = None,
        default_model: Optional[str] = None,
        log_dir: Optional[Path] = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.mini_agent_cmd = mini_agent_cmd
        self.max_steps = max_steps
        self.openai_timeout = openai_timeout
        self.max_new_tokens = max_new_tokens
        self.error_log_dir = error_log_dir
        self.default_model = default_model
        self.log_dir = log_dir

    # ------------------------------------------------------------------
    # Public API (same signature as OpenCodeClient.run)
    # ------------------------------------------------------------------

    def run(
        self,
        agent_name: str,
        system_prompt: str,
        user_message: str,
        file_paths: Optional[List[Path]] = None,
        model: Optional[str] = None,
        timeout: int = 300,
        read_allowed: bool = True,
        bash_allowed: bool = False,
        on_event: Optional[Callable[[dict], None]] = None,
        workspace_dir: Optional[Path] = None,
    ) -> str:
        """
        Invoke mini-coding-agent-CLI in one-shot mode and return the response.

        Parameters mirror :meth:`OpenCodeClient.run`.  The ``workspace_dir``
        parameter (not present on OpenCodeClient) sets ``--cwd``; when omitted
        the current working directory is used.

        Emits synthetic ``{"type": "text", "part": {"text": line}}`` events via
        *on_event* for each non-empty line of stdout, preserving compatibility
        with streaming progress callbacks.

        Raises
        ------
        AgentError
            If the subprocess times out or exits with a non-zero code.
        """
        effective_model = model or self.default_model or "moonshot-v1-128k"

        # Build task: prepend system prompt, optionally inline files.
        task_parts: List[str] = []
        if system_prompt.strip():
            task_parts.append(system_prompt.rstrip())
            task_parts.append("\n\n---\n\n")

        # When read is disabled (e.g. synthesis round), inline all file contents.
        if not read_allowed and file_paths:
            task_parts.append("**Attached files (full content):**\n")
            for fp in file_paths:
                task_parts.append(f"\n### {fp.name}\n")
                try:
                    task_parts.append(fp.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    task_parts.append(f"[unreadable binary file: {fp}]")
            task_parts.append("\n\n---\n\n")

        task_parts.append(user_message)
        full_task = "".join(task_parts)

        # Build --allow list: each category is a separate argument
        allow_cats: List[str] = []
        if read_allowed:
            allow_cats.append("read")
        if bash_allowed:
            allow_cats.append("bash")
        if not allow_cats:
            allow_cats = ["read"]

        # CWD for the agent
        cwd_arg = str((workspace_dir or Path(".")).resolve())

        cmd = [
            self.mini_agent_cmd,
            "--backend", "openai",
            "--openai-api-key", self.api_key,
            "--openai-base-url", self.base_url,
            "--openai-timeout", str(self.openai_timeout),
            "--model", effective_model,
            "--approval", "auto",
            "--allow", *allow_cats,
            "--max-steps", str(self.max_steps),
            "--max-new-tokens", str(self.max_new_tokens),
            "--cwd", cwd_arg,
            full_task,
        ]

        started_at = datetime.now(timezone.utc).isoformat()
        start_time = time.monotonic()

        stdout_lines: List[str] = []
        stderr_chunks: List[str] = []
        timed_out = False
        returncode: Optional[int] = None

        try:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                duration = time.monotonic() - start_time
                self._write_error(agent_name, f"mini-coding-agent not found: {exc}")
                self._write_audit_log(
                    agent_name=agent_name, model=effective_model,
                    started_at=started_at, duration_seconds=duration,
                    timeout=timeout, exit_code=None,
                    stdout="", stderr=str(exc), response_text_length=0,
                    error=f"FileNotFoundError: {exc}",
                    user_message=user_message, system_prompt=system_prompt,
                )
                raise AgentError(
                    f"mini-coding-agent not found on PATH (cmd={self.mini_agent_cmd!r}). "
                    "Install it or set MINI_AGENT_CMD in .env."
                ) from exc

            def _drain_stderr() -> None:
                stderr_chunks.append(proc.stderr.read())

            stderr_th = threading.Thread(target=_drain_stderr, daemon=True)
            stderr_th.start()

            def _kill_on_timeout() -> None:
                nonlocal timed_out
                timed_out = True
                proc.kill()

            timer = threading.Timer(timeout, _kill_on_timeout)
            timer.start()

            try:
                for raw_line in proc.stdout:
                    line = raw_line.rstrip("\n")
                    stdout_lines.append(line)
                    if line.strip() and on_event:
                        try:
                            on_event({"type": "text", "part": {"text": line + "\n"}})
                        except Exception:  # noqa: BLE001
                            pass
            finally:
                pass

            proc.stdout.close()
            proc.wait()
            timer.cancel()
            stderr_th.join(timeout=5)
            returncode = proc.returncode

        finally:
            pass  # no temp files to clean up (unlike OpenCodeClient)

        duration = time.monotonic() - start_time
        stdout = "\n".join(stdout_lines)
        stderr_text = stderr_chunks[0] if stderr_chunks else ""

        if timed_out:
            self._write_error(agent_name, f"Timeout after {timeout}s")
            self._write_audit_log(
                agent_name=agent_name, model=effective_model,
                started_at=started_at, duration_seconds=duration,
                timeout=timeout, exit_code=None,
                stdout=stdout, stderr=stderr_text, response_text_length=0,
                error=f"TimeoutExpired after {timeout}s",
                user_message=user_message, system_prompt=system_prompt,
            )
            raise AgentError(f"Agent '{agent_name}' timed out after {timeout}s")

        if returncode != 0:
            self._write_error(agent_name, stderr_text)
            self._write_audit_log(
                agent_name=agent_name, model=effective_model,
                started_at=started_at, duration_seconds=duration,
                timeout=timeout, exit_code=returncode,
                stdout=stdout, stderr=stderr_text, response_text_length=0,
                error=f"exit code {returncode}",
                user_message=user_message, system_prompt=system_prompt,
            )
            raise AgentError(
                f"Agent '{agent_name}' failed (exit {returncode}). "
                "See error log for details."
            )

        response_text = self._extract_text(stdout)
        self._write_audit_log(
            agent_name=agent_name, model=effective_model,
            started_at=started_at, duration_seconds=duration,
            timeout=timeout, exit_code=returncode,
            stdout=stdout, stderr=stderr_text,
            response_text_length=len(response_text), error=None,
            user_message=user_message, system_prompt=system_prompt,
        )
        return response_text

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_text(self, stdout: str) -> str:
        """Extract the agent's final answer from mini-agent stdout.

        Strips the mini-coding-agent ASCII banner first, then looks for the
        last ``<final>…</final>`` block; falls back to the remaining stdout.
        """
        cleaned = _strip_mini_agent_banner(stdout).strip()
        matches = _FINAL_TAG_RE.findall(cleaned)
        if matches:
            return matches[-1].strip()
        return cleaned

    def _write_error(self, agent_name: str, message: str) -> None:
        if self.error_log_dir is None:
            return
        self.error_log_dir.mkdir(parents=True, exist_ok=True)
        try:
            (self.error_log_dir / f"{agent_name}.log").write_text(
                message, encoding="utf-8"
            )
        except OSError:
            pass

    def _write_audit_log(
        self,
        agent_name: str,
        model: Optional[str],
        started_at: str,
        duration_seconds: float,
        timeout: int,
        exit_code: Optional[int],
        stdout: str,
        stderr: str,
        response_text_length: int,
        error: Optional[str],
        user_message: str = "",
        system_prompt: str = "",
    ) -> None:
        if self.log_dir is None:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ts = started_at[:19].replace(":", "-").replace(" ", "T")
        filename = f"{ts}_{agent_name}.json"
        record = {
            "agent_name": agent_name,
            "backend": "mini_agent",
            "model": model,
            "started_at": started_at,
            "duration_seconds": round(duration_seconds, 3),
            "timeout": timeout,
            "exit_code": exit_code,
            "stdout_size": len(stdout),
            "response_text_length": response_text_length,
            "error": error,
            "system_prompt": system_prompt,
            "user_message": user_message,
            "stdout": stdout,
            "stderr": stderr,
        }
        try:
            (self.log_dir / filename).write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Factory — select backend from config + .env
# ---------------------------------------------------------------------------

def make_agent_client(
    cfg: Dict,
    error_log_dir: Optional[Path] = None,
    log_dir: Optional[Path] = None,
    env_file: Optional[Path] = None,
) -> Union[OpenCodeClient, MiniAgentClient]:
    """Create the appropriate agent client based on system.yaml and .env.

    Parameters
    ----------
    cfg:
        The ``opencode`` section of ``system.yaml`` (already parsed as dict).
    error_log_dir:
        Where per-agent error logs are written.
    log_dir:
        Where per-call audit JSON logs are written.
    env_file:
        Explicit path to a ``.env`` file; forwarded to :func:`load_env`.

    Priority for backend selection
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    1. ``backend`` key in *cfg* (system.yaml ``opencode.backend``)
    2. ``AGENT_BACKEND`` environment variable / .env
    3. Default: ``"opencode"``

    Priority for default model
    ~~~~~~~~~~~~~~~~~~~~~~~~~~
    1. ``default_model`` key in *cfg*
    2. ``LLM_MODEL`` in .env (or provider default)
    """
    from src.env_config import load_env  # local import to avoid circular deps

    llm = load_env(env_file)

    # Backend: yaml cfg > env > "opencode"
    backend = (cfg.get("backend") or llm.agent_backend or "opencode").lower().strip()

    # Model: yaml cfg > env (which already applied provider default)
    default_model: Optional[str] = cfg.get("default_model") or llm.model or None

    if backend == "mini_agent":
        if not llm.api_key:
            raise AgentError(
                "mini_agent backend requires an API key. "
                "Set KIMI_API_KEY (or LLM_API_KEY) in your .env file."
            )
        return MiniAgentClient(
            api_key=llm.api_key,
            base_url=llm.base_url,
            mini_agent_cmd=llm.mini_agent_cmd,
            max_steps=llm.mini_agent_max_steps,
            openai_timeout=llm.mini_agent_openai_timeout,
            max_new_tokens=llm.mini_agent_max_new_tokens,
            error_log_dir=error_log_dir,
            default_model=default_model,
            log_dir=log_dir,
        )

    # Default: opencode
    return OpenCodeClient(
        error_log_dir=error_log_dir,
        default_model=default_model,
        log_dir=log_dir,
    )
