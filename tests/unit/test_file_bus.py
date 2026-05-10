"""
Unit tests for src/file_bus.py
"""

import json
from pathlib import Path

import pytest

from src.file_bus import FileBus, WORKSPACE_DIR_NAME
from src.scanner import Manifest, FileEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bus(tmp_path: Path) -> FileBus:
    return FileBus(tmp_path)


def _sample_manifest(case_id: str = "test_case") -> Manifest:
    m = Manifest(case_id=case_id, timestamp="2025-01-01T00:00:00Z")
    m.files = [
        FileEntry(
            path="report.md",
            size=100,
            mime_type="text/markdown",
            preview="# report",
            checksum="abc123",
        )
    ]
    m.total_files = len(m.files)
    return m


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFileBusPaths:
    def test_workspace_dir_under_case_dir(self, tmp_path: Path):
        bus = _make_bus(tmp_path)
        assert bus.workspace_dir == tmp_path / WORKSPACE_DIR_NAME

    def test_opinions_dir_path(self, tmp_path: Path):
        bus = _make_bus(tmp_path)
        assert bus.opinions_dir == bus.workspace_dir / "03_opinions"

    def test_errors_dir_path(self, tmp_path: Path):
        bus = _make_bus(tmp_path)
        assert bus.errors_dir == bus.workspace_dir / "errors"

    def test_artifact_paths(self, tmp_path: Path):
        bus = _make_bus(tmp_path)
        ws = bus.workspace_dir
        assert bus.manifest_path == ws / "00_manifest.json"
        assert bus.index_path    == ws / "01_index.json"
        assert bus.dispatch_path == ws / "02_dispatch.json"
        assert bus.debate_path   == ws / "04_debate.json"
        assert bus.report_path   == ws / "05_mdt_report.md"


class TestFileBusWorkspaceInit:
    def test_init_workspace_creates_dirs(self, tmp_path: Path):
        bus = _make_bus(tmp_path)
        assert not bus.workspace_dir.exists()
        bus.init_workspace()
        assert bus.workspace_dir.exists()
        assert bus.opinions_dir.exists()
        assert bus.errors_dir.exists()

    def test_init_workspace_is_idempotent(self, tmp_path: Path):
        bus = _make_bus(tmp_path)
        bus.init_workspace()
        # Second call must not raise
        bus.init_workspace()
        assert bus.workspace_dir.exists()


class TestFileBusWriteRead:
    def setup_method(self):
        pass

    def test_save_and_load_manifest(self, tmp_path: Path):
        bus = _make_bus(tmp_path)
        bus.init_workspace()
        m = _sample_manifest()
        bus.save_manifest(m)
        assert bus.manifest_path.exists()
        data = bus.load_manifest()
        assert data["case_id"] == "test_case"
        assert data["total_files"] == 1

    def test_save_and_load_index(self, tmp_path: Path):
        bus = _make_bus(tmp_path)
        bus.init_workspace()
        index = {"files_classified": [{"file": "a.md", "category": "病历"}]}
        bus.save_index(index)
        loaded = bus.load_index()
        assert loaded["files_classified"][0]["category"] == "病历"

    def test_save_and_load_dispatch(self, tmp_path: Path):
        bus = _make_bus(tmp_path)
        bus.init_workspace()
        dispatch = {"specialists_required": [{"name": "影像科", "files_assigned": []}]}
        bus.save_dispatch(dispatch)
        loaded = bus.load_dispatch()
        assert loaded["specialists_required"][0]["name"] == "影像科"

    def test_save_opinion_creates_file(self, tmp_path: Path):
        bus = _make_bus(tmp_path)
        bus.init_workspace()
        opinion_path = bus.save_opinion("影像科", "影像所见：…")
        assert opinion_path.exists()
        assert opinion_path.read_text(encoding="utf-8") == "影像所见：…"

    def test_load_opinions_returns_all(self, tmp_path: Path):
        bus = _make_bus(tmp_path)
        bus.init_workspace()
        bus.save_opinion("影像科", "影像意见")
        bus.save_opinion("病理科", "病理意见")
        opinions = bus.load_opinions()
        assert len(opinions) == 2
        assert opinions["影像科"] == "影像意见"
        assert opinions["病理科"] == "病理意见"

    def test_load_opinions_empty_dir(self, tmp_path: Path):
        bus = _make_bus(tmp_path)
        bus.init_workspace()
        opinions = bus.load_opinions()
        assert opinions == {}

    def test_save_report(self, tmp_path: Path):
        bus = _make_bus(tmp_path)
        bus.init_workspace()
        bus.save_report("# MDT Report\n\n最终结论：…")
        assert bus.report_path.exists()
        content = bus.report_path.read_text(encoding="utf-8")
        assert "MDT Report" in content

    def test_save_debate(self, tmp_path: Path):
        bus = _make_bus(tmp_path)
        bus.init_workspace()
        debate = {"disagreements": []}
        bus.save_debate(debate)
        loaded_raw = json.loads(bus.debate_path.read_text(encoding="utf-8"))
        assert "disagreements" in loaded_raw

    def test_json_files_use_chinese_chars(self, tmp_path: Path):
        bus = _make_bus(tmp_path)
        bus.init_workspace()
        data = {"name": "影像科", "value": "正常"}
        bus.save_index(data)
        raw = bus.index_path.read_text(encoding="utf-8")
        # ensure_ascii=False means Chinese chars appear as-is, not escaped
        assert "影像科" in raw
        assert "\\u" not in raw
