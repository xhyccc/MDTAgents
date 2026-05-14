"""
env_config.py — Load .env configuration and resolve LLM / agent-backend settings.

Priority for locating the .env file:
  1. ``env_file`` argument passed to ``load_env()``
  2. ``MDT_ENV_FILE`` environment variable (explicit override)
  3. ``.env`` in the current working directory
  4. ``.env`` two levels above this source file (project root)

Resolved settings are returned as an ``LLMConfig`` named-tuple.  All fields
have safe defaults so callers work even when no .env file is present.

Supported LLM providers (OpenAI-compatible)
-------------------------------------------
  kimi        — https://api.moonshot.cn/v1          (default model: moonshot-v1-128k)
  moonshot    — https://api.moonshot.cn/v1          (alias for kimi)
  zhipu       — https://open.bigmodel.cn/api/paas/v4  (default model: glm-4)
  siliconflow — https://api.siliconflow.cn/v1       (default model: deepseek-ai/DeepSeek-V3)
  openai      — https://api.openai.com/v1           (default model: gpt-4o)
  custom      — set LLM_BASE_URL + LLM_API_KEY + LLM_MODEL explicitly

Supported agent backends
------------------------
  opencode   — opencode CLI v1.4.3+  (default)
  mini_agent — mini-coding-agent-CLI (https://github.com/xhyccc/mini-coding-agent-CLI)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple, Optional

# ---------------------------------------------------------------------------
# Provider tables
# ---------------------------------------------------------------------------

PROVIDER_BASE_URLS: dict[str, str] = {
    "kimi":        "https://api.moonshot.cn/v1",
    "moonshot":    "https://api.moonshot.cn/v1",
    "zhipu":       "https://open.bigmodel.cn/api/paas/v4",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "openai":      "https://api.openai.com/v1",
}

PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "kimi":        "moonshot-v1-128k",
    "moonshot":    "moonshot-v1-128k",
    "zhipu":       "glm-4",
    "siliconflow": "deepseek-ai/DeepSeek-V3",
    "openai":      "gpt-4o",
}

# Environment variable name that holds the API key for each provider.
PROVIDER_KEY_ENV_VARS: dict[str, str] = {
    "kimi":        "KIMI_API_KEY",
    "moonshot":    "MOONSHOT_API_KEY",
    "zhipu":       "ZHIPU_API_KEY",
    "siliconflow": "SILICONFLOW_API_KEY",
    "openai":      "OPENAI_API_KEY",
}

SUPPORTED_BACKENDS = ("opencode", "mini_agent")


# ---------------------------------------------------------------------------
# Resolved config
# ---------------------------------------------------------------------------

class LLMConfig(NamedTuple):
    """Fully-resolved LLM and agent-backend configuration."""
    provider: str               # e.g. "kimi", "openai", "custom"
    api_key: Optional[str]      # active API key; None if not set
    base_url: str               # OpenAI-compatible endpoint URL
    model: Optional[str]        # model identifier; None → caller uses its default
    agent_backend: str          # "opencode" or "mini_agent"
    mini_agent_cmd: str         # command / path for mini-coding-agent binary
    mini_agent_max_steps: int   # --max-steps value for mini-coding-agent
    mini_agent_openai_timeout: int   # --openai-timeout (seconds per API call)
    mini_agent_max_new_tokens: int   # --max-new-tokens per model step


# ---------------------------------------------------------------------------
# Minimal .env file parser (no third-party dependency required)
# ---------------------------------------------------------------------------

def _load_dotenv_file(path: Path) -> None:
    """Parse *path* as a .env file and populate ``os.environ``.

    Rules:
    * Blank lines and lines starting with ``#`` are skipped.
    * Lines must contain ``=``; anything after a bare ``#`` is a comment.
    * Values wrapped in matching ``"..."`` or ``'...'`` have the quotes stripped.
    * Existing environment variables are **not** overwritten (matches dotenv's
      default ``override=False`` behaviour).
    * Raises nothing — missing or unreadable files are silently ignored.
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        # Strip inline comments (only outside quotes for simplicity)
        raw = raw.split("#", 1)[0].strip()
        # Unwrap matching surrounding quotes
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
            raw = raw[1:-1]
        if key and key not in os.environ:
            os.environ[key] = raw


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_env(env_file: Optional[Path] = None) -> LLMConfig:
    """Load .env (if present) and return a resolved :class:`LLMConfig`.

    Parameters
    ----------
    env_file:
        Explicit path to a ``.env`` file.  When provided it is loaded first and
        overrides auto-discovery.
    """
    # 1. Explicit file argument
    if env_file is not None:
        _load_dotenv_file(env_file)
    elif (explicit := os.environ.get("MDT_ENV_FILE", "").strip()):
        # 2. MDT_ENV_FILE env var
        _load_dotenv_file(Path(explicit))
    else:
        # 3. Auto-discover: CWD first, then project root
        project_root = Path(__file__).parent.parent
        for candidate in (Path.cwd() / ".env", project_root / ".env"):
            if candidate.is_file():
                _load_dotenv_file(candidate)
                break

    # --- provider ---
    provider = os.environ.get("LLM_PROVIDER", "kimi").lower().strip()

    # --- base_url ---
    base_url = (
        os.environ.get("LLM_BASE_URL", "").strip()
        or PROVIDER_BASE_URLS.get(provider, "https://api.openai.com/v1")
    )

    # --- API key: LLM_API_KEY > provider-specific var > None ---
    api_key: Optional[str] = os.environ.get("LLM_API_KEY", "").strip() or None
    if api_key is None:
        key_var = PROVIDER_KEY_ENV_VARS.get(provider, "OPENAI_API_KEY")
        api_key = os.environ.get(key_var, "").strip() or None

    # --- model: LLM_MODEL > provider default ---
    model: Optional[str] = os.environ.get("LLM_MODEL", "").strip() or None
    if model is None:
        model = PROVIDER_DEFAULT_MODELS.get(provider)

    # --- agent backend ---
    backend_raw = os.environ.get("AGENT_BACKEND", "opencode").lower().strip()
    agent_backend = backend_raw if backend_raw in SUPPORTED_BACKENDS else "opencode"

    # --- mini-agent settings ---
    mini_agent_cmd = (
        os.environ.get("MINI_AGENT_CMD", "").strip() or "mini-coding-agent"
    )
    mini_agent_max_steps_raw = os.environ.get("MINI_AGENT_MAX_STEPS", "15").strip()
    try:
        mini_agent_max_steps = int(mini_agent_max_steps_raw)
    except ValueError:
        mini_agent_max_steps = 15

    mini_agent_openai_timeout_raw = os.environ.get("MINI_AGENT_OPENAI_TIMEOUT", "300").strip()
    try:
        mini_agent_openai_timeout = int(mini_agent_openai_timeout_raw)
    except ValueError:
        mini_agent_openai_timeout = 300

    mini_agent_max_new_tokens_raw = os.environ.get("MINI_AGENT_MAX_NEW_TOKENS", "8192").strip()
    try:
        mini_agent_max_new_tokens = int(mini_agent_max_new_tokens_raw)
    except ValueError:
        mini_agent_max_new_tokens = 8192

    return LLMConfig(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        agent_backend=agent_backend,
        mini_agent_cmd=mini_agent_cmd,
        mini_agent_max_steps=mini_agent_max_steps,
        mini_agent_openai_timeout=mini_agent_openai_timeout,
        mini_agent_max_new_tokens=mini_agent_max_new_tokens,
    )
