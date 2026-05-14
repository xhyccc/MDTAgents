"""
Unit tests for src/coordinator.py
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.coordinator import Coordinator, _extract_json, _render, _available_specialist_names
from src.file_bus import FileBus
from src.scanner import Manifest, FileEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_CONFIG = {
    "opencode": {
        "default_model": "test-model",
        "timeout": 60,
    },
    "specialists": [
        {"name": "影像科", "model": "test-model"},
        {"name": "病理科", "model": "test-model"},
    ],
}

SAMPLE_INDEX = {
    "files_classified": [
        {"file": "CT检查.md", "category": "影像"},
        {"file": "病理报告.md", "category": "病理"},
    ],
    "completeness": "complete",
    "summary": "两份资料齐全",
}

SAMPLE_DISPATCH = {
    "specialists_required": [
        {"name": "影像科", "files_assigned": ["CT检查.md"]},
        {"name": "病理科", "files_assigned": ["病理报告.md"]},
    ]
}


def _make_config(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config" / "system.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.dump(MINIMAL_CONFIG, allow_unicode=True), encoding="utf-8")
    return cfg_path


def _make_prompts(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "coordinator_index.md").write_text("Index prompt: {case_dir} {total_files} {manifest_json}", encoding="utf-8")
    (prompts_dir / "coordinator_dispatch.md").write_text("Dispatch prompt: {index_json} {available_specialists_json}", encoding="utf-8")
    (prompts_dir / "coordinator_index_dispatch.md").write_text(
        "IndexDispatch prompt: {case_dir} {total_files} {manifest_json} {available_specialists_json}",
        encoding="utf-8",
    )
    (prompts_dir / "coordinator_synthesis.md").write_text("Synthesis prompt: {index_json} {opinions_json}", encoding="utf-8")

    spec_dir = prompts_dir / "specialists"
    spec_dir.mkdir()
    (spec_dir / "base.md").write_text("# Base specialist prompt", encoding="utf-8")
    (spec_dir / "影像科.md").write_text("# 影像科 prompt", encoding="utf-8")
    (spec_dir / "病理科.md").write_text("# 病理科 prompt", encoding="utf-8")
    return prompts_dir


def _make_bus(tmp_path: Path, case_name: str = "test_case") -> FileBus:
    case_dir = tmp_path / case_name
    case_dir.mkdir(exist_ok=True)
    (case_dir / "CT检查.md").write_text("CT所见：正常", encoding="utf-8")
    bus = FileBus(case_dir)
    bus.init_workspace()
    return bus


def _make_coordinator(tmp_path: Path) -> Coordinator:
    bus = _make_bus(tmp_path)
    cfg_path = _make_config(tmp_path)
    prompts_dir = _make_prompts(tmp_path)
    return Coordinator(bus=bus, config_path=cfg_path, prompts_dir=prompts_dir)


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_plain_json(self):
        raw = '{"key": "value"}'
        result = _extract_json(raw)
        assert result["key"] == "value"

    def test_json_in_markdown_fence(self):
        raw = "```json\n{\"key\": \"value\"}\n```"
        result = _extract_json(raw)
        assert result["key"] == "value"

    def test_json_in_unnamed_fence(self):
        raw = "```\n{\"a\": 1}\n```"
        result = _extract_json(raw)
        assert result["a"] == 1

    def test_json_with_text_preamble(self):
        raw = "Here is the JSON:\n{\"x\": true}"
        result = _extract_json(raw)
        assert result["x"] is True

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError, match="Could not parse JSON"):
            _extract_json("this is not json at all")

    def test_nested_json(self):
        data = {"nested": {"a": [1, 2, 3]}}
        raw = json.dumps(data)
        result = _extract_json(raw)
        assert result["nested"]["a"] == [1, 2, 3]

    def test_chinese_content_preserved(self):
        raw = '{"专科": "影像科", "结论": "正常"}'
        result = _extract_json(raw)
        assert result["专科"] == "影像科"


# ---------------------------------------------------------------------------
# _render
# ---------------------------------------------------------------------------

class TestRender:
    def test_simple_substitution(self):
        template = "Hello {name}!"
        result = _render(template, name="World")
        assert result == "Hello World!"

    def test_multiple_substitutions(self):
        template = "{a} + {b} = {c}"
        result = _render(template, a="1", b="2", c="3")
        assert result == "1 + 2 = 3"

    def test_missing_key_left_as_is(self):
        template = "Hello {name} {surname}!"
        result = _render(template, name="Zhang")
        assert "{surname}" in result

    def test_non_string_values(self):
        template = "Count: {n}"
        result = _render(template, n=42)
        assert result == "Count: 42"


# ---------------------------------------------------------------------------
# _available_specialist_names
# ---------------------------------------------------------------------------

class TestAvailableSpecialistNames:
    def test_returns_specialist_names(self, tmp_path: Path):
        spec_dir = tmp_path / "specialists"
        spec_dir.mkdir()
        (spec_dir / "影像科.md").write_text("...", encoding="utf-8")
        (spec_dir / "病理科.md").write_text("...", encoding="utf-8")
        (spec_dir / "base.md").write_text("...", encoding="utf-8")  # excluded

        names = _available_specialist_names(tmp_path)
        assert "影像科" in names
        assert "病理科" in names
        assert "base" not in names

    def test_empty_dir_returns_empty_list(self, tmp_path: Path):
        (tmp_path / "specialists").mkdir()
        names = _available_specialist_names(tmp_path)
        assert names == []

    def test_nonexistent_dir_returns_empty_list(self, tmp_path: Path):
        names = _available_specialist_names(tmp_path)
        assert names == []


# ---------------------------------------------------------------------------
# Coordinator.run_index
# ---------------------------------------------------------------------------

class TestCoordinatorRunIndex:
    def test_run_index_saves_and_returns_index(self, tmp_path: Path):
        coordinator = _make_coordinator(tmp_path)
        index_json = json.dumps(SAMPLE_INDEX)

        with patch.object(coordinator.client, "run", return_value=index_json):
            result = coordinator.run_index(Manifest(
                case_id="test",
                timestamp="2025-01-01T00:00:00Z",
                files=[],
                total_files=0,
            ))

        assert result["completeness"] == "complete"
        assert coordinator.bus.index_path.exists()

    def test_run_index_passes_manifest_to_prompt(self, tmp_path: Path):
        coordinator = _make_coordinator(tmp_path)
        manifest = Manifest(
            case_id="sample",
            timestamp="2025-01-01T00:00:00Z",
            files=[FileEntry(path="a.md", size=10, mime_type="text/plain", preview="hi", checksum="abc")],
            total_files=1,
        )
        calls = []
        with patch.object(coordinator.client, "run", side_effect=lambda **kw: calls.append(kw) or json.dumps(SAMPLE_INDEX)):
            coordinator.run_index(manifest)

        user_msg = calls[0]["user_message"]
        assert "a.md" in user_msg or "sample" in user_msg  # manifest info included


# ---------------------------------------------------------------------------
# Coordinator.run_dispatch
# ---------------------------------------------------------------------------

class TestCoordinatorRunDispatch:
    def test_run_dispatch_saves_and_returns_dispatch(self, tmp_path: Path):
        coordinator = _make_coordinator(tmp_path)
        dispatch_json = json.dumps(SAMPLE_DISPATCH)

        with patch.object(coordinator.client, "run", return_value=dispatch_json):
            result = coordinator.run_dispatch(SAMPLE_INDEX)

        assert len(result["specialists_required"]) == 2
        assert coordinator.bus.dispatch_path.exists()

    def test_run_dispatch_passes_read_not_allowed(self, tmp_path: Path):
        """Dispatcher must not attempt to read files — all context is in index JSON."""
        coordinator = _make_coordinator(tmp_path)
        calls: list = []

        def capture(**kw):
            calls.append(kw)
            return json.dumps(SAMPLE_DISPATCH)

        with patch.object(coordinator.client, "run", side_effect=capture):
            coordinator.run_dispatch(SAMPLE_INDEX)

        assert calls, "client.run was never called"
        assert calls[0].get("read_allowed") is False


# ---------------------------------------------------------------------------
# Coordinator.run_synthesis
# ---------------------------------------------------------------------------

class TestCoordinatorRunSynthesis:
    def test_run_synthesis_saves_report(self, tmp_path: Path):
        coordinator = _make_coordinator(tmp_path)
        opinions = {
            "影像科": "影像所见正常。",
            "病理科": "病理无异常。",
        }
        report_text = "# MDT Final Report\n\n最终结论：患者病情稳定。"

        with patch.object(coordinator.client, "run", return_value=report_text):
            result = coordinator.run_synthesis(SAMPLE_INDEX, opinions)

        assert result == report_text
        assert coordinator.bus.report_path.exists()
        saved = coordinator.bus.report_path.read_text(encoding="utf-8")
        assert "MDT Final Report" in saved

    def test_run_synthesis_has_bash_and_read_allowed(self, tmp_path: Path):
        """Synthesis uses inline data only; bash and read are disabled."""
        coordinator = _make_coordinator(tmp_path)
        calls: list = []

        def capture(**kw):
            calls.append(kw)
            return "Final report."

        with patch.object(coordinator.client, "run", side_effect=capture):
            coordinator.run_synthesis(SAMPLE_INDEX, {"影像科": "正常", "病理科": "无异常"})

        assert calls, "client.run was never called"
        assert calls[0].get("read_allowed") is False
        assert calls[0].get("bash_allowed") is False


# ---------------------------------------------------------------------------
# Coordinator.run_index (read_allowed + file_texts injection)
# ---------------------------------------------------------------------------

class TestCoordinatorRunIndexReadAllowed:
    def test_run_index_passes_read_not_allowed(self, tmp_path: Path):
        """Index agent receives all content in the prompt; file reading is disabled."""
        coordinator = _make_coordinator(tmp_path)
        calls: list = []

        def capture(**kw):
            calls.append(kw)
            return json.dumps(SAMPLE_INDEX)

        manifest = Manifest(
            case_id="t", timestamp="2025-01-01T00:00:00Z", files=[], total_files=0
        )
        with patch.object(coordinator.client, "run", side_effect=capture):
            coordinator.run_index(manifest)

        assert calls, "client.run was never called"
        assert calls[0].get("read_allowed") is False


# ---------------------------------------------------------------------------
# Coordinator._build_file_texts
# ---------------------------------------------------------------------------

class TestBuildFileTexts:
    """Unit tests for the _build_file_texts helper method."""

    def test_returns_content_from_md_file(self, tmp_path: Path):
        """Plain .md files in the case dir should appear in the output."""
        coordinator = _make_coordinator(tmp_path)
        # _make_coordinator places CT检查.md with content "CT所见：正常"
        result = coordinator._build_file_texts()
        assert "CT所见：正常" in result

    def test_section_header_format(self, tmp_path: Path):
        """Each file's section should be prefixed with '=== <rel_path> ==='."""
        coordinator = _make_coordinator(tmp_path)
        result = coordinator._build_file_texts()
        assert "===" in result and "CT检查.md" in result

    def test_prefers_context_txt_over_raw_file(self, tmp_path: Path):
        """When a context .txt exists it must be used instead of the raw file."""
        coordinator = _make_coordinator(tmp_path)
        bus = coordinator.bus

        # Create a context .txt with different content for CT检查.md
        ctx_dir = bus.file_context_dir(bus.case_dir / "CT检查.md")
        ctx_dir.mkdir(parents=True, exist_ok=True)
        (ctx_dir / "CT检查.txt").write_text("EXTRACTED: 胸部CT未见明显异常", encoding="utf-8")

        result = coordinator._build_file_texts()
        assert "EXTRACTED: 胸部CT未见明显异常" in result
        assert "CT所见：正常" not in result  # raw .md content should be replaced

    def test_skips_files_in_workspace_dir(self, tmp_path: Path):
        """Files inside .mdt_workspace/ must never appear in the output."""
        coordinator = _make_coordinator(tmp_path)
        bus = coordinator.bus

        # Plant a file inside the workspace dir
        workspace_file = bus.workspace_dir / "secret.md"
        workspace_file.write_text("应该被忽略的内容", encoding="utf-8")

        result = coordinator._build_file_texts()
        assert "应该被忽略的内容" not in result


# ---------------------------------------------------------------------------
# Coordinator.run_index_and_dispatch
# ---------------------------------------------------------------------------

SAMPLE_COMBINED = {
    "file_classifications": [
        {"path": "CT检查.md", "category": "影像", "confidence": 0.95, "reason": "CT图像"},
    ],
    "case_completeness": {
        "has_imaging": True,
        "has_pathology": False,
        "has_labs": False,
        "has_history": False,
        "has_previous_treatment": False,
        "missing_key_categories": ["病理"],
    },
    "summary": "仅有影像资料",
    "specialists_required": [
        {"name": "影像科", "reason": "有CT", "files_assigned": ["CT检查.md"]},
    ],
    "notes": ["缺少病理"],
}


class TestCoordinatorRunIndexAndDispatch:
    def test_saves_both_files_and_returns_tuple(self, tmp_path: Path):
        coordinator = _make_coordinator(tmp_path)
        combined_json = json.dumps(SAMPLE_COMBINED)

        with patch.object(coordinator.client, "run", return_value=combined_json):
            index, dispatch = coordinator.run_index_and_dispatch(
                Manifest(case_id="t", timestamp="2025-01-01T00:00:00Z", files=[], total_files=0)
            )

        assert "file_classifications" in index
        assert "case_completeness" in index
        assert "summary" in index
        assert "specialists_required" in dispatch
        assert "notes" in dispatch
        assert coordinator.bus.index_path.exists()
        assert coordinator.bus.dispatch_path.exists()

    def test_only_one_llm_call(self, tmp_path: Path):
        coordinator = _make_coordinator(tmp_path)
        calls: list = []

        def capture(**kw):
            calls.append(kw)
            return json.dumps(SAMPLE_COMBINED)

        with patch.object(coordinator.client, "run", side_effect=capture):
            coordinator.run_index_and_dispatch(
                Manifest(case_id="t", timestamp="2025-01-01T00:00:00Z", files=[], total_files=0)
            )

        assert len(calls) == 1, f"Expected 1 LLM call, got {len(calls)}"

    def test_read_not_allowed(self, tmp_path: Path):
        coordinator = _make_coordinator(tmp_path)
        calls: list = []

        with patch.object(coordinator.client, "run",
                          side_effect=lambda **kw: calls.append(kw) or json.dumps(SAMPLE_COMBINED)):
            coordinator.run_index_and_dispatch(
                Manifest(case_id="t", timestamp="2025-01-01T00:00:00Z", files=[], total_files=0)
            )

        assert calls[0].get("read_allowed") is False

    def test_cache_skips_llm_call(self, tmp_path: Path):
        coordinator = _make_coordinator(tmp_path)
        # Pre-populate cache
        coordinator.bus.save_index({"file_classifications": [], "summary": "cached"})
        coordinator.bus.save_dispatch({"specialists_required": []})

        calls: list = []
        with patch.object(coordinator.client, "run", side_effect=lambda **kw: calls.append(kw)):
            index, dispatch = coordinator.run_index_and_dispatch(
                Manifest(case_id="t", timestamp="2025-01-01T00:00:00Z", files=[], total_files=0)
            )

        assert len(calls) == 0, "LLM must not be called when cache exists"
        assert index["summary"] == "cached"

    def test_index_and_dispatch_keys_split_correctly(self, tmp_path: Path):
        coordinator = _make_coordinator(tmp_path)
        with patch.object(coordinator.client, "run", return_value=json.dumps(SAMPLE_COMBINED)):
            index, dispatch = coordinator.run_index_and_dispatch(
                Manifest(case_id="t", timestamp="2025-01-01T00:00:00Z", files=[], total_files=0)
            )

        # Index must NOT contain dispatch-only keys
        assert "specialists_required" not in index
        assert "notes" not in index
        # Dispatch must NOT contain index-only keys
        assert "file_classifications" not in dispatch
        assert "case_completeness" not in dispatch

    def test_manifest_included_in_user_message(self, tmp_path: Path):
        coordinator = _make_coordinator(tmp_path)
        calls: list = []
        manifest = Manifest(
            case_id="unique_id_xyz",
            timestamp="2025-01-01T00:00:00Z",
            files=[],
            total_files=0,
        )

        with patch.object(coordinator.client, "run",
                          side_effect=lambda **kw: calls.append(kw) or json.dumps(SAMPLE_COMBINED)):
            coordinator.run_index_and_dispatch(manifest)

        user_msg = calls[0]["user_message"]
        assert "unique_id_xyz" in user_msg


class TestBuildFileTexts:
    def test_skips_unsupported_extensions(self, tmp_path: Path):
        """Binary/unsupported files (e.g. .png) must not appear in the output."""
        coordinator = _make_coordinator(tmp_path)
        (coordinator.bus.case_dir / "photo.png").write_bytes(b"\x89PNG\r\n")

        result = coordinator._build_file_texts()
        # No section header for .png should appear
        assert "photo.png" not in result

    def test_multiple_files_all_included(self, tmp_path: Path):
        """All supported files in the case dir should produce separate sections."""
        coordinator = _make_coordinator(tmp_path)
        (coordinator.bus.case_dir / "血常规.md").write_text("白细胞：5.0", encoding="utf-8")
        (coordinator.bus.case_dir / "病理报告.md").write_text("腺癌", encoding="utf-8")

        result = coordinator._build_file_texts()
        assert "CT所见：正常" in result
        assert "白细胞：5.0" in result
        assert "腺癌" in result

    def test_empty_case_returns_placeholder(self, tmp_path: Path):
        """A case dir with no supported text files returns the placeholder string."""
        # Create a fresh coordinator whose case dir has no supported files.
        case_dir = tmp_path / "empty_case"
        case_dir.mkdir()
        cfg_path = _make_config(tmp_path)
        prompts_dir = _make_prompts(tmp_path)
        bus = FileBus(case_dir)
        bus.init_workspace()
        coordinator = Coordinator(bus=bus, config_path=cfg_path, prompts_dir=prompts_dir)

        result = coordinator._build_file_texts()
        assert result == "(no text content found)"


# ---------------------------------------------------------------------------
# coordinator_timeout and retry
# ---------------------------------------------------------------------------

class TestCoordinatorTimeoutAndRetry:
    """Coordinator rounds use coordinator_timeout, not timeout; retries on AgentError."""

    def _make_coordinator_with_retry_config(self, tmp_path: Path, retries: int = 2) -> Coordinator:
        bus = _make_bus(tmp_path)
        cfg_path = tmp_path / "config" / "system.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg = {
            "opencode": {
                "default_model": "test-model",
                "timeout": 600,
                "coordinator_timeout": 120,
                "synthesis_timeout": 300,
                "coordinator_retries": retries,
            },
            "specialists": [{"name": "影像科"}, {"name": "病理科"}],
        }
        cfg_path.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
        prompts_dir = _make_prompts(tmp_path)
        return Coordinator(bus=bus, config_path=cfg_path, prompts_dir=prompts_dir)

    def test_coordinator_timeout_loaded_from_config(self, tmp_path: Path):
        coordinator = self._make_coordinator_with_retry_config(tmp_path)
        assert coordinator.coordinator_timeout == 120
        assert coordinator.synthesis_timeout == 300
        assert coordinator.timeout == 600  # original timeout unchanged

    def test_coordinator_retries_loaded_from_config(self, tmp_path: Path):
        coordinator = self._make_coordinator_with_retry_config(tmp_path, retries=3)
        assert coordinator.coordinator_retries == 3

    def test_run_index_uses_coordinator_timeout(self, tmp_path: Path):
        coordinator = self._make_coordinator_with_retry_config(tmp_path)
        calls: list = []
        manifest = Manifest(case_id="x", timestamp="t", files=[], total_files=0)

        with patch.object(coordinator.client, "run",
                          side_effect=lambda **kw: calls.append(kw) or json.dumps(SAMPLE_INDEX)):
            coordinator.run_index(manifest)

        assert calls[0]["timeout"] == 120

    def test_run_dispatch_uses_coordinator_timeout(self, tmp_path: Path):
        coordinator = self._make_coordinator_with_retry_config(tmp_path)
        calls: list = []

        with patch.object(coordinator.client, "run",
                          side_effect=lambda **kw: calls.append(kw) or json.dumps(SAMPLE_DISPATCH)):
            coordinator.run_dispatch(SAMPLE_INDEX)

        assert calls[0]["timeout"] == 120

    def test_run_synthesis_uses_synthesis_timeout(self, tmp_path: Path):
        coordinator = self._make_coordinator_with_retry_config(tmp_path)
        calls: list = []

        with patch.object(coordinator.client, "run",
                          side_effect=lambda **kw: calls.append(kw) or "Final report."):
            coordinator.run_synthesis(SAMPLE_INDEX, {"影像科": "正常"})

        assert calls[0]["timeout"] == 300  # synthesis_timeout, not coordinator_timeout

    def test_run_with_retry_succeeds_on_second_attempt(self, tmp_path: Path):
        from src.cli_client import AgentError
        coordinator = self._make_coordinator_with_retry_config(tmp_path, retries=3)
        attempt = [0]

        def _flaky():
            attempt[0] += 1
            if attempt[0] < 2:
                raise AgentError("temporary failure")
            return "success"

        result = coordinator._run_with_retry(_flaky, step_name="test", retry_delay=0)
        assert result == "success"
        assert attempt[0] == 2

    def test_run_with_retry_raises_after_max_attempts(self, tmp_path: Path):
        from src.cli_client import AgentError
        coordinator = self._make_coordinator_with_retry_config(tmp_path, retries=2)
        calls = [0]

        def _always_fail():
            calls[0] += 1
            raise AgentError("always fails")

        with pytest.raises(AgentError, match="always fails"):
            coordinator._run_with_retry(_always_fail, step_name="test", retry_delay=0)
        assert calls[0] == 2

    def test_run_dispatch_retries_on_agent_error(self, tmp_path: Path):
        from src.cli_client import AgentError
        coordinator = self._make_coordinator_with_retry_config(tmp_path, retries=3)
        call_count = [0]

        def _flaky_dispatch(**kw):
            call_count[0] += 1
            if call_count[0] < 3:
                raise AgentError("Kimi API timeout")
            return json.dumps(SAMPLE_DISPATCH)

        with patch.object(coordinator.client, "run", side_effect=_flaky_dispatch):
            result = coordinator.run_dispatch(SAMPLE_INDEX)

        assert call_count[0] == 3
        assert "specialists_required" in result

    def test_coordinator_timeout_defaults_to_timeout_if_not_set(self, tmp_path: Path):
        """If coordinator_timeout not in config, falls back to timeout."""
        coordinator = _make_coordinator(tmp_path)  # MINIMAL_CONFIG has no coordinator_timeout
        assert coordinator.coordinator_timeout == coordinator.timeout

    def test_synthesis_timeout_defaults_to_coordinator_timeout(self, tmp_path: Path):
        """If synthesis_timeout not set, falls back to coordinator_timeout."""
        bus = _make_bus(tmp_path)
        cfg_path = tmp_path / "config" / "system.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg = {
            "opencode": {
                "default_model": "test-model",
                "timeout": 600,
                "coordinator_timeout": 120,
                # synthesis_timeout intentionally absent
            },
            "specialists": [],
        }
        cfg_path.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
        coordinator = Coordinator(bus=bus, config_path=cfg_path, prompts_dir=_make_prompts(tmp_path))
        assert coordinator.synthesis_timeout == 120

    def test_synthesis_timeout_defaults_to_timeout_when_no_coordinator_timeout(self, tmp_path: Path):
        """If neither synthesis_timeout nor coordinator_timeout set, falls back to timeout."""
        coordinator = _make_coordinator(tmp_path)  # MINIMAL_CONFIG only has timeout: 60
        assert coordinator.synthesis_timeout == coordinator.timeout
