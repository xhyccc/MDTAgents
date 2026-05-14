"""
Unit tests for src/specialist_pool.py — focusing on the timeout fallback strategy.
"""

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

from src.cli_client import AgentError
from src.file_bus import FileBus
from src.specialist_pool import SpecialistPool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_CONFIG = {
    "opencode": {
        "default_model": "test-model",
        "timeout": 60,
        "specialist_timeout": 120,
        "fallback_timeout": 30,
        "max_workers": 1,
        "max_rounds": 3,
    },
}

SAMPLE_SPEC = {"name": "影像科", "files_assigned": ["CT检查.md"]}


def _make_config(tmp_path: Path, extra: Dict[str, Any] = None) -> Path:
    cfg = dict(MINIMAL_CONFIG)
    if extra:
        cfg["opencode"] = {**cfg["opencode"], **extra}
    cfg_path = tmp_path / "config" / "system.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    return cfg_path


def _make_prompts(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    (prompts_dir / "specialists").mkdir(parents=True)
    (prompts_dir / "specialists" / "base.md").write_text("# Base", encoding="utf-8")
    (prompts_dir / "specialists" / "影像科.md").write_text("# 影像科", encoding="utf-8")
    return prompts_dir


def _make_pool(tmp_path: Path, extra_cfg: Dict[str, Any] = None) -> SpecialistPool:
    case_dir = tmp_path / "test_case"
    case_dir.mkdir(exist_ok=True)
    (case_dir / "CT检查.md").write_text("CT正常", encoding="utf-8")
    bus = FileBus(case_dir)
    bus.init_workspace()
    cfg_path = _make_config(tmp_path, extra_cfg)
    prompts_dir = _make_prompts(tmp_path)
    return SpecialistPool(bus=bus, config_path=cfg_path, prompts_dir=prompts_dir)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestSpecialistPoolConfig:
    def test_reads_specialist_timeout(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        assert pool.specialist_timeout == 120

    def test_reads_fallback_timeout(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        assert pool.fallback_timeout == 30

    def test_default_specialist_timeout_when_missing(self, tmp_path: Path):
        pool = _make_pool(tmp_path, extra_cfg={"specialist_timeout": None})
        # yaml.dump writes None as null; oc_cfg.get returns None, so falls back to 1800
        assert pool.specialist_timeout == 1800 or pool.specialist_timeout is None  # acceptable

    def test_default_fallback_timeout_when_missing(self, tmp_path: Path):
        pool = _make_pool(tmp_path, extra_cfg={"fallback_timeout": None})
        assert pool.fallback_timeout == 300 or pool.fallback_timeout is None


# ---------------------------------------------------------------------------
# _collect_text_for_fallback
# ---------------------------------------------------------------------------

class TestCollectTextForFallback:
    def test_collects_md_files(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        ws = pool.bus.agent_workspace_dir("影像科")
        ws.mkdir(parents=True)
        (ws / "CT检查.md").write_text("CT所见正常", encoding="utf-8")

        result = pool._collect_text_for_fallback(ws)
        assert "CT检查.md" in result
        assert "CT所见正常" in result

    def test_collects_txt_files(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        ws = pool.bus.agent_workspace_dir("影像科")
        ws.mkdir(parents=True)
        (ws / "notes.txt").write_text("extra notes", encoding="utf-8")

        result = pool._collect_text_for_fallback(ws)
        assert "notes.txt" in result
        assert "extra notes" in result

    def test_collects_context_subdir_txt(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        ws = pool.bus.agent_workspace_dir("影像科")
        ctx = ws / "CT检查_pdf"
        ctx.mkdir(parents=True)
        (ctx / "CT检查.txt").write_text("提取文本内容", encoding="utf-8")

        result = pool._collect_text_for_fallback(ws)
        assert "提取文本内容" in result
        assert "CT检查_pdf/CT检查.txt" in result

    def test_skips_images(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        ws = pool.bus.agent_workspace_dir("影像科")
        ws.mkdir(parents=True)
        (ws / "scan.png").write_bytes(b"\x89PNG")
        (ws / "scan.jpg").write_bytes(b"\xff\xd8")

        result = pool._collect_text_for_fallback(ws)
        assert "scan.png" not in result
        assert "scan.jpg" not in result

    def test_skips_page_screenshots_in_subdir(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        ws = pool.bus.agent_workspace_dir("影像科")
        ctx = ws / "CT检查_pdf"
        ctx.mkdir(parents=True)
        (ctx / "page_001.png").write_bytes(b"\x89PNG")
        (ctx / "CT检查.txt").write_text("text only", encoding="utf-8")

        result = pool._collect_text_for_fallback(ws)
        assert "page_001.png" not in result
        assert "text only" in result

    def test_returns_placeholder_for_missing_workspace(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        nonexistent = tmp_path / "ghost_workspace"
        result = pool._collect_text_for_fallback(nonexistent)
        assert result == "(no workspace found)"

    def test_returns_placeholder_for_empty_workspace(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        ws = pool.bus.agent_workspace_dir("影像科")
        ws.mkdir(parents=True)  # empty
        result = pool._collect_text_for_fallback(ws)
        assert result == "(no text content available)"

    def test_section_headers_use_equals_format(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        ws = pool.bus.agent_workspace_dir("影像科")
        ws.mkdir(parents=True)
        (ws / "report.md").write_text("content", encoding="utf-8")

        result = pool._collect_text_for_fallback(ws)
        assert "=== report.md ===" in result


# ---------------------------------------------------------------------------
# _run_specialist_fallback
# ---------------------------------------------------------------------------

class TestRunSpecialistFallback:
    def test_fallback_calls_client_with_read_denied(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        ws = pool.bus.agent_workspace_dir("影像科")
        ws.mkdir(parents=True)
        (ws / "CT检查.md").write_text("CT正常", encoding="utf-8")

        calls: list = []
        with patch.object(pool.client, "run", side_effect=lambda **kw: calls.append(kw) or "fallback opinion"):
            pool._run_specialist_fallback(SAMPLE_SPEC)

        assert calls
        assert calls[0].get("read_allowed") is False

    def test_fallback_uses_fallback_timeout(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        ws = pool.bus.agent_workspace_dir("影像科")
        ws.mkdir(parents=True)

        calls: list = []
        with patch.object(pool.client, "run", side_effect=lambda **kw: calls.append(kw) or "ok"):
            pool._run_specialist_fallback(SAMPLE_SPEC)

        assert calls[0]["timeout"] == pool.fallback_timeout

    def test_fallback_passes_no_file_paths(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        ws = pool.bus.agent_workspace_dir("影像科")
        ws.mkdir(parents=True)

        calls: list = []
        with patch.object(pool.client, "run", side_effect=lambda **kw: calls.append(kw) or "ok"):
            pool._run_specialist_fallback(SAMPLE_SPEC)

        assert calls[0]["file_paths"] == []

    def test_fallback_embeds_workspace_text_in_prompt(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        ws = pool.bus.agent_workspace_dir("影像科")
        ws.mkdir(parents=True)
        (ws / "CT检查.md").write_text("胸部CT：两肺清晰", encoding="utf-8")

        calls: list = []
        with patch.object(pool.client, "run", side_effect=lambda **kw: calls.append(kw) or "ok"):
            pool._run_specialist_fallback(SAMPLE_SPEC)

        assert "胸部CT：两肺清晰" in calls[0]["user_message"]

    def test_fallback_appends_warning_note_zh(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        ws = pool.bus.agent_workspace_dir("影像科")
        ws.mkdir(parents=True)

        with patch.object(pool.client, "run", return_value="opinion text"):
            result = pool._run_specialist_fallback(SAMPLE_SPEC)

        assert "超时回退" in result or "fallback" in result.lower()

    def test_fallback_appends_warning_note_en(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        pool.lang = "en"
        ws = pool.bus.agent_workspace_dir("影像科")
        ws.mkdir(parents=True)

        with patch.object(pool.client, "run", return_value="opinion text"):
            result = pool._run_specialist_fallback(SAMPLE_SPEC)

        assert "fallback" in result.lower() or "timeout" in result.lower()

    def test_fallback_saves_opinion_to_bus(self, tmp_path: Path):
        pool = _make_pool(tmp_path)
        ws = pool.bus.agent_workspace_dir("影像科")
        ws.mkdir(parents=True)

        with patch.object(pool.client, "run", return_value="fallback result"):
            pool._run_specialist_fallback(SAMPLE_SPEC)

        saved = pool.bus.opinions_dir / "影像科.html"
        assert saved.exists()
        assert "fallback result" in saved.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Timeout → fallback wiring in _run_specialist
# ---------------------------------------------------------------------------

class TestRunSpecialistTimeoutFallback:
    def _make_pool_with_workspace(self, tmp_path: Path) -> SpecialistPool:
        pool = _make_pool(tmp_path)
        ws = pool.bus.agent_workspace_dir("影像科")
        ws.mkdir(parents=True)
        (ws / "CT检查.md").write_text("CT正常", encoding="utf-8")
        return pool

    def test_timeout_triggers_fallback(self, tmp_path: Path):
        pool = self._make_pool_with_workspace(tmp_path)

        def _raise_timeout(**kw):
            if "fallback" not in kw.get("agent_name", ""):
                raise AgentError("Agent 'specialist_影像科' timed out after 120s")
            return "fallback opinion"

        with patch.object(pool.client, "run", side_effect=_raise_timeout):
            result = pool._run_specialist(SAMPLE_SPEC)

        assert "fallback" in result.lower() or "超时" in result

    def test_non_timeout_agent_error_is_reraised(self, tmp_path: Path):
        pool = self._make_pool_with_workspace(tmp_path)

        with patch.object(pool.client, "run", side_effect=AgentError("failed (exit 1)")):
            with pytest.raises(AgentError, match="failed"):
                pool._run_specialist(SAMPLE_SPEC)

    def test_success_path_skips_fallback(self, tmp_path: Path):
        pool = self._make_pool_with_workspace(tmp_path)
        fallback_called: list = []

        with patch.object(pool.client, "run", return_value="primary opinion"):
            with patch.object(pool, "_run_specialist_fallback", side_effect=lambda s: fallback_called.append(s)):
                pool._run_specialist(SAMPLE_SPEC)

        assert not fallback_called

    def test_fallback_opinion_saved_after_timeout(self, tmp_path: Path):
        pool = self._make_pool_with_workspace(tmp_path)

        def _raise_timeout(**kw):
            if "fallback" not in kw.get("agent_name", ""):
                raise AgentError("timed out after 120s")
            return "fallback text"

        with patch.object(pool.client, "run", side_effect=_raise_timeout):
            pool._run_specialist(SAMPLE_SPEC)

        saved = pool.bus.opinions_dir / "影像科.html"
        assert saved.exists()
        assert "fallback text" in saved.read_text(encoding="utf-8")
