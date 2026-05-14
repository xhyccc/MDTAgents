"""
Unit tests for src/env_config.py
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.env_config import (
    LLMConfig,
    PROVIDER_BASE_URLS,
    PROVIDER_DEFAULT_MODELS,
    PROVIDER_KEY_ENV_VARS,
    _load_dotenv_file,
    load_env,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _isolated_env(monkeypatch, keep: tuple[str, ...] = ()):
    """Remove all relevant env vars so tests start from a clean slate."""
    keys_to_remove = [
        "LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
        "AGENT_BACKEND", "MINI_AGENT_CMD", "MINI_AGENT_MAX_STEPS",
        "MINI_AGENT_OPENAI_TIMEOUT", "MINI_AGENT_MAX_NEW_TOKENS",
        "MDT_ENV_FILE",
        "KIMI_API_KEY", "MOONSHOT_API_KEY", "ZHIPU_API_KEY",
        "SILICONFLOW_API_KEY", "OPENAI_API_KEY",
    ]
    for k in keys_to_remove:
        if k not in keep:
            monkeypatch.delenv(k, raising=False)
    # Block auto-discovery of the project's .env file so tests are hermetic
    if "MDT_ENV_FILE" not in keep:
        monkeypatch.setenv("MDT_ENV_FILE", "/nonexistent/.env")


# ---------------------------------------------------------------------------
# _load_dotenv_file
# ---------------------------------------------------------------------------

class TestLoadDotenvFile:
    def test_basic_key_value(self, tmp_path: Path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text('FOO_VAR=hello\nBAR_VAR=world\n')
        monkeypatch.delenv("FOO_VAR", raising=False)
        monkeypatch.delenv("BAR_VAR", raising=False)
        _load_dotenv_file(env_file)
        assert os.environ["FOO_VAR"] == "hello"
        assert os.environ["BAR_VAR"] == "world"

    def test_quoted_double_quotes(self, tmp_path: Path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text('QUOTED_VAR="my value"\n')
        monkeypatch.delenv("QUOTED_VAR", raising=False)
        _load_dotenv_file(env_file)
        assert os.environ["QUOTED_VAR"] == "my value"

    def test_quoted_single_quotes(self, tmp_path: Path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("SINGLE_Q='abc'\n")
        monkeypatch.delenv("SINGLE_Q", raising=False)
        _load_dotenv_file(env_file)
        assert os.environ["SINGLE_Q"] == "abc"

    def test_inline_comment_stripped(self, tmp_path: Path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("WITH_COMMENT=value  # this is a comment\n")
        monkeypatch.delenv("WITH_COMMENT", raising=False)
        _load_dotenv_file(env_file)
        assert os.environ["WITH_COMMENT"] == "value"

    def test_comment_lines_skipped(self, tmp_path: Path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("# this is a full-line comment\nACTUAL=yes\n")
        monkeypatch.delenv("ACTUAL", raising=False)
        _load_dotenv_file(env_file)
        assert os.environ["ACTUAL"] == "yes"

    def test_does_not_overwrite_existing_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("EXISTING_KEY", "original")
        env_file = tmp_path / ".env"
        env_file.write_text("EXISTING_KEY=overwritten\n")
        _load_dotenv_file(env_file)
        assert os.environ["EXISTING_KEY"] == "original"

    def test_missing_file_is_silently_ignored(self, tmp_path: Path):
        # Should not raise
        _load_dotenv_file(tmp_path / "nonexistent.env")

    def test_blank_lines_skipped(self, tmp_path: Path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("\n\n   \nKEY_BLANK=val\n\n")
        monkeypatch.delenv("KEY_BLANK", raising=False)
        _load_dotenv_file(env_file)
        assert os.environ["KEY_BLANK"] == "val"

    def test_line_without_equals_skipped(self, tmp_path: Path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("NOEQUALSSIGN\nOK_KEY=ok\n")
        monkeypatch.delenv("OK_KEY", raising=False)
        _load_dotenv_file(env_file)
        assert os.environ.get("NOEQUALSSIGN") is None
        assert os.environ["OK_KEY"] == "ok"


# ---------------------------------------------------------------------------
# load_env — provider resolution
# ---------------------------------------------------------------------------

class TestLoadEnvProviderResolution:
    def test_kimi_defaults(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "kimi")
        monkeypatch.setenv("KIMI_API_KEY", "sk-test-kimi")
        cfg = load_env()
        assert cfg.provider == "kimi"
        assert cfg.api_key == "sk-test-kimi"
        assert cfg.base_url == PROVIDER_BASE_URLS["kimi"]
        assert cfg.model == PROVIDER_DEFAULT_MODELS["kimi"]

    def test_moonshot_alias(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "moonshot")
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-ms")
        cfg = load_env()
        assert cfg.base_url == PROVIDER_BASE_URLS["moonshot"]
        assert cfg.api_key == "sk-ms"

    def test_zhipu_provider(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "zhipu")
        monkeypatch.setenv("ZHIPU_API_KEY", "zp-key")
        cfg = load_env()
        assert cfg.base_url == PROVIDER_BASE_URLS["zhipu"]
        assert cfg.model == PROVIDER_DEFAULT_MODELS["zhipu"]
        assert cfg.api_key == "zp-key"

    def test_siliconflow_provider(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "siliconflow")
        monkeypatch.setenv("SILICONFLOW_API_KEY", "sf-key")
        cfg = load_env()
        assert cfg.base_url == PROVIDER_BASE_URLS["siliconflow"]
        assert cfg.model == PROVIDER_DEFAULT_MODELS["siliconflow"]

    def test_openai_provider(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
        cfg = load_env()
        assert cfg.base_url == PROVIDER_BASE_URLS["openai"]
        assert cfg.api_key == "sk-oai"

    def test_custom_provider(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "custom")
        monkeypatch.setenv("LLM_BASE_URL", "https://my.api/v1")
        monkeypatch.setenv("LLM_API_KEY", "custom-key")
        monkeypatch.setenv("LLM_MODEL", "my-model")
        cfg = load_env()
        assert cfg.base_url == "https://my.api/v1"
        assert cfg.api_key == "custom-key"
        assert cfg.model == "my-model"

    def test_llm_api_key_overrides_provider_specific(self, monkeypatch):
        """LLM_API_KEY takes priority over KIMI_API_KEY."""
        _isolated_env(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "kimi")
        monkeypatch.setenv("KIMI_API_KEY", "sk-kimi")
        monkeypatch.setenv("LLM_API_KEY", "sk-generic")
        cfg = load_env()
        assert cfg.api_key == "sk-generic"

    def test_llm_model_override(self, monkeypatch):
        """LLM_MODEL overrides the provider default."""
        _isolated_env(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "kimi")
        monkeypatch.setenv("LLM_MODEL", "moonshot-v1-32k")
        cfg = load_env()
        assert cfg.model == "moonshot-v1-32k"

    def test_missing_api_key_returns_none(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "kimi")
        cfg = load_env()
        assert cfg.api_key is None

    def test_default_provider_is_kimi(self, monkeypatch):
        _isolated_env(monkeypatch)
        cfg = load_env()
        assert cfg.provider == "kimi"


# ---------------------------------------------------------------------------
# load_env — agent backend
# ---------------------------------------------------------------------------

class TestLoadEnvBackend:
    def test_default_backend_is_opencode(self, monkeypatch):
        _isolated_env(monkeypatch)
        cfg = load_env()
        assert cfg.agent_backend == "opencode"

    def test_mini_agent_backend(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("AGENT_BACKEND", "mini_agent")
        cfg = load_env()
        assert cfg.agent_backend == "mini_agent"

    def test_invalid_backend_falls_back_to_opencode(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("AGENT_BACKEND", "nonsense_backend")
        cfg = load_env()
        assert cfg.agent_backend == "opencode"

    def test_mini_agent_cmd_default(self, monkeypatch):
        _isolated_env(monkeypatch)
        cfg = load_env()
        assert cfg.mini_agent_cmd == "mini-coding-agent"

    def test_mini_agent_cmd_custom(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("MINI_AGENT_CMD", "uv run mini-coding-agent")
        cfg = load_env()
        assert cfg.mini_agent_cmd == "uv run mini-coding-agent"

    def test_mini_agent_max_steps_default(self, monkeypatch):
        _isolated_env(monkeypatch)
        cfg = load_env()
        assert cfg.mini_agent_max_steps == 15

    def test_mini_agent_max_steps_custom(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("MINI_AGENT_MAX_STEPS", "20")
        cfg = load_env()
        assert cfg.mini_agent_max_steps == 20

    def test_mini_agent_max_steps_invalid_uses_default(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("MINI_AGENT_MAX_STEPS", "not-a-number")
        cfg = load_env()
        assert cfg.mini_agent_max_steps == 15

    def test_mini_agent_openai_timeout_default(self, monkeypatch):
        _isolated_env(monkeypatch)
        cfg = load_env()
        assert cfg.mini_agent_openai_timeout == 300

    def test_mini_agent_openai_timeout_custom(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("MINI_AGENT_OPENAI_TIMEOUT", "600")
        cfg = load_env()
        assert cfg.mini_agent_openai_timeout == 600

    def test_mini_agent_openai_timeout_invalid_uses_default(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("MINI_AGENT_OPENAI_TIMEOUT", "bad")
        cfg = load_env()
        assert cfg.mini_agent_openai_timeout == 300

    def test_mini_agent_max_new_tokens_default(self, monkeypatch):
        _isolated_env(monkeypatch)
        cfg = load_env()
        assert cfg.mini_agent_max_new_tokens == 8192

    def test_mini_agent_max_new_tokens_custom(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("MINI_AGENT_MAX_NEW_TOKENS", "16384")
        cfg = load_env()
        assert cfg.mini_agent_max_new_tokens == 16384

    def test_mini_agent_max_new_tokens_invalid_uses_default(self, monkeypatch):
        _isolated_env(monkeypatch)
        monkeypatch.setenv("MINI_AGENT_MAX_NEW_TOKENS", "nope")
        cfg = load_env()
        assert cfg.mini_agent_max_new_tokens == 8192


# ---------------------------------------------------------------------------
# load_env — .env file loading
# ---------------------------------------------------------------------------

class TestLoadEnvFromFile:
    def test_reads_env_from_explicit_file(self, tmp_path: Path, monkeypatch):
        _isolated_env(monkeypatch)
        env_file = tmp_path / ".env"
        env_file.write_text(
            "LLM_PROVIDER=siliconflow\n"
            "SILICONFLOW_API_KEY=sf-from-file\n"
            "AGENT_BACKEND=mini_agent\n"
        )
        cfg = load_env(env_file=env_file)
        assert cfg.provider == "siliconflow"
        assert cfg.api_key == "sf-from-file"
        assert cfg.agent_backend == "mini_agent"

    def test_env_file_does_not_override_existing_env(self, tmp_path: Path, monkeypatch):
        """Environment variables already set must not be overwritten by .env file."""
        _isolated_env(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        env_file = tmp_path / ".env"
        env_file.write_text("LLM_PROVIDER=zhipu\n")
        cfg = load_env(env_file=env_file)
        assert cfg.provider == "openai"  # env var wins

    def test_missing_env_file_does_not_raise(self, tmp_path: Path, monkeypatch):
        _isolated_env(monkeypatch)
        cfg = load_env(env_file=tmp_path / "nonexistent.env")
        # Should return defaults without raising
        assert isinstance(cfg, LLMConfig)

    def test_mdt_env_file_env_var(self, tmp_path: Path, monkeypatch):
        _isolated_env(monkeypatch)
        env_file = tmp_path / "custom.env"
        env_file.write_text("LLM_PROVIDER=zhipu\nZHIPU_API_KEY=zp-via-mdt-env\n")
        monkeypatch.setenv("MDT_ENV_FILE", str(env_file))
        cfg = load_env()
        assert cfg.provider == "zhipu"
        assert cfg.api_key == "zp-via-mdt-env"

    def test_returns_llmconfig_namedtuple(self, monkeypatch):
        _isolated_env(monkeypatch)
        cfg = load_env()
        assert isinstance(cfg, LLMConfig)
        # All fields accessible by name
        _ = cfg.provider, cfg.api_key, cfg.base_url, cfg.model
        _ = cfg.agent_backend, cfg.mini_agent_cmd, cfg.mini_agent_max_steps
        _ = cfg.mini_agent_openai_timeout, cfg.mini_agent_max_new_tokens
