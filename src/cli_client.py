"""
cli_client.py — Thin wrapper around the OpenCode CLI.

All Agent invocations go through this module. Errors are written to
.mdt_workspace/errors/{agent_name}.log and surfaced as AgentError.

Compatibility: opencode v1.4.3+. Uses per-call agent markdown files placed in
.opencode/agents/ to pass system prompts, and --format json for clean output.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import List, Optional


class AgentError(RuntimeError):
    """Raised when an OpenCode CLI call fails."""


class OpenCodeClient:
    """
    Wraps ``opencode run`` CLI invocations (opencode v1.4.3+).

    Each call writes a temporary agent markdown file into ``.opencode/agents/``
    so the system prompt is applied correctly, then invokes
    ``opencode run --agent <name> --format json`` and parses the JSON event
    stream to extract the assistant text.
    """

    AGENTS_DIR = Path(".opencode") / "agents"

    def __init__(self, error_log_dir: Optional[Path] = None, default_model: Optional[str] = None):
        self.error_log_dir = error_log_dir
        self.default_model = default_model

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
    ) -> str:
        """
        Call ``opencode run`` with the supplied parameters.

        Parameters
        ----------
        agent_name:
            Logical name used in error-log filenames (e.g. "coordinator_index").
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
            Use False for coordinator agents that have all content embedded in
            the prompt; use True (default) for specialist agents that need to
            read files from their workspace.

        Returns
        -------
        str
            The assistant's text response extracted from the JSON event stream.

        Raises
        ------
        AgentError
            If the process exits with a non-zero code.
        """
        effective_model = model or self.default_model

        # Write a temporary agent markdown file with the system prompt.
        # Agent files placed in .opencode/agents/ are auto-discovered by opencode.
        self.AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        temp_agent_name = f"mdt_{agent_name}_{uuid.uuid4().hex[:8]}"
        agent_path = self.AGENTS_DIR / f"{temp_agent_name}.md"

        read_perm = "allow" if read_allowed else "deny"
        frontmatter = (
            "---\n"
            "description: Temporary MDT agent\n"
            "mode: primary\n"
            "permission:\n"
            "  bash: deny\n"
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

        result = None
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self._write_error(agent_name, f"Timeout after {timeout}s\n{exc}")
            raise AgentError(
                f"Agent '{agent_name}' timed out after {timeout}s"
            ) from exc
        except FileNotFoundError as exc:
            self._write_error(agent_name, f"opencode CLI not found: {exc}")
            raise AgentError(
                "opencode CLI not found on PATH. Please install it first."
            ) from exc
        finally:
            agent_path.unlink(missing_ok=True)

        if result.returncode != 0:
            stderr_text = result.stderr or ""
            self._write_error(agent_name, stderr_text)
            raise AgentError(
                f"Agent '{agent_name}' failed (exit {result.returncode}). "
                f"See error log for details."
            )

        return self._extract_text(result.stdout)

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
