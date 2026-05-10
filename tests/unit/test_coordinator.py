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
