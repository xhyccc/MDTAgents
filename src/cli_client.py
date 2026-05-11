"""
cli_client.py — Thin wrapper around the OpenCode CLI.

All Agent invocations go through this module. Errors are written to
.mdt_workspace/errors/{agent_name}.log and surfaced as AgentError.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional


class AgentError(RuntimeError):
    """Raised when an OpenCode CLI call fails."""


class OpenCodeClient:
    """
    Wraps ``opencode run`` CLI invocations.

    Each call creates a temporary file for the system prompt (so we can pass
    arbitrary-length prompts on the command line) and collects stdout as the
    Agent's response.
    """

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
    ) -> str:
        """
        Call ``opencode run`` with the supplied parameters.

        Parameters
        ----------
        agent_name:
            Logical name used in error-log filenames (e.g. "coordinator_index").
        system_prompt:
            Full system-prompt text (written to a temp file internally).
        user_message:
            The user turn message sent to the model.
        file_paths:
            Optional list of files to attach (``--file`` flags).
        model:
            Model override; falls back to *self.default_model*.
        timeout:
            Maximum seconds to wait for the subprocess.

        Returns
        -------
        str
            The decoded stdout from the CLI process.

        Raises
        ------
        AgentError
            If the process exits with a non-zero code.
        """
        effective_model = model or self.default_model

        # Write system prompt to a temp file so we can pass a path flag
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as sp_file:
            sp_file.write(system_prompt)
            sp_path = sp_file.name

        cmd = [
            "opencode",
            "run",
        ]
        if effective_model:
            cmd += ["--model", effective_model]
        cmd += [
            "--system-prompt", sp_path,
            "--message", user_message,
        ]

        for fp in (file_paths or []):
            cmd += ["--file", str(fp)]

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
            # Clean up the temp system-prompt file
            Path(sp_path).unlink(missing_ok=True)

        if result.returncode != 0:
            stderr_text = result.stderr or ""
            self._write_error(agent_name, stderr_text)
            raise AgentError(
                f"Agent '{agent_name}' failed (exit {result.returncode}). "
                f"See error log for details."
            )

        return result.stdout

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_error(self, agent_name: str, message: str) -> None:
        if self.error_log_dir is None:
            return
        self.error_log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.error_log_dir / f"{agent_name}.log"
        try:
            log_path.write_text(message, encoding="utf-8")
        except OSError:
            pass
