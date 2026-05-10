"""
Unit tests for src/scanner.py
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from src.scanner import Scanner, Manifest, FileEntry, PREVIEW_CHARS, WORKSPACE_DIR_NAME


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_case(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a temporary case directory with the given {filename: content} map."""
    for name, content in files.items():
        fp = tmp_path / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestScannerBasic:
    def test_scan_empty_dir(self, tmp_path: Path):
        scanner = Scanner()
        manifest = scanner.scan(tmp_path)
        assert manifest.total_files == 0
        assert manifest.files == []
        assert manifest.case_id == tmp_path.name

    def test_scan_single_md(self, tmp_path: Path):
        content = "# 患者信息\n姓名：张三\n年龄：60岁"
        case_dir = _make_case(tmp_path, {"入院记录.md": content})
        scanner = Scanner()
        manifest = scanner.scan(case_dir)
        assert manifest.total_files == 1
        entry = manifest.files[0]
        assert entry.path == "入院记录.md"
        assert entry.preview == content[:PREVIEW_CHARS]
        assert len(entry.checksum) == 32  # MD5 hex

    def test_scan_multiple_supported_formats(self, tmp_path: Path):
        case_dir = _make_case(tmp_path, {
            "a.md": "markdown content",
            "b.txt": "plain text",
            "c.json": '{"key": "value"}',
            "d.csv": "col1,col2\n1,2",
        })
        scanner = Scanner()
        manifest = scanner.scan(case_dir)
        assert manifest.total_files == 4

    def test_scan_unsupported_files_excluded(self, tmp_path: Path):
        case_dir = _make_case(tmp_path, {
            "a.md": "content",
            "b.png": "binary data",
            "c.mp4": "video",
        })
        scanner = Scanner()
        manifest = scanner.scan(case_dir)
        assert manifest.total_files == 1
        assert manifest.files[0].path == "a.md"

    def test_workspace_dir_excluded(self, tmp_path: Path):
        case_dir = tmp_path
        # Create a normal file
        (case_dir / "report.md").write_text("hello", encoding="utf-8")
        # Create a file inside .mdt_workspace
        ws = case_dir / WORKSPACE_DIR_NAME
        ws.mkdir()
        (ws / "00_manifest.json").write_text("{}", encoding="utf-8")

        scanner = Scanner()
        manifest = scanner.scan(case_dir)
        # Only report.md should be counted — workspace files are excluded
        assert manifest.total_files == 1
        assert all(WORKSPACE_DIR_NAME not in f.path for f in manifest.files)

    def test_preview_truncated_to_preview_chars(self, tmp_path: Path):
        long_content = "A" * (PREVIEW_CHARS * 3)
        case_dir = _make_case(tmp_path, {"long.txt": long_content})
        scanner = Scanner()
        manifest = scanner.scan(case_dir)
        assert len(manifest.files[0].preview) == PREVIEW_CHARS

    def test_timestamp_present(self, tmp_path: Path):
        scanner = Scanner()
        manifest = scanner.scan(tmp_path)
        assert manifest.timestamp  # non-empty string
        # Should be an ISO timestamp
        assert "T" in manifest.timestamp

    def test_checksum_is_stable(self, tmp_path: Path):
        case_dir = _make_case(tmp_path, {"stable.txt": "fixed content"})
        scanner = Scanner()
        m1 = scanner.scan(case_dir)
        m2 = scanner.scan(case_dir)
        assert m1.files[0].checksum == m2.files[0].checksum

    def test_checksum_changes_with_content(self, tmp_path: Path):
        f = tmp_path / "file.txt"
        f.write_text("version 1", encoding="utf-8")
        scanner = Scanner()
        m1 = scanner.scan(tmp_path)

        f.write_text("version 2", encoding="utf-8")
        m2 = scanner.scan(tmp_path)

        assert m1.files[0].checksum != m2.files[0].checksum

    def test_scan_nested_subdirectory(self, tmp_path: Path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "nested.md").write_text("nested", encoding="utf-8")
        (tmp_path / "top.md").write_text("top", encoding="utf-8")

        scanner = Scanner()
        manifest = scanner.scan(tmp_path)
        assert manifest.total_files == 2
        paths = {f.path for f in manifest.files}
        assert "top.md" in paths
        assert os.path.join("subdir", "nested.md") in paths


class TestManifestSerialization:
    def test_to_dict_contains_expected_keys(self, tmp_path: Path):
        case_dir = _make_case(tmp_path, {"x.md": "data"})
        manifest = Scanner().scan(case_dir)
        d = manifest.to_dict()
        assert "case_id" in d
        assert "files" in d
        assert "total_files" in d
        assert "timestamp" in d

    def test_to_json_round_trip(self, tmp_path: Path):
        case_dir = _make_case(tmp_path, {"x.txt": "hello"})
        manifest = Scanner().scan(case_dir)
        json_str = manifest.to_json()
        parsed = json.loads(json_str)
        assert parsed["total_files"] == 1

    def test_file_entry_fields(self, tmp_path: Path):
        content = "test content"
        case_dir = _make_case(tmp_path, {"test.txt": content})
        manifest = Scanner().scan(case_dir)
        entry = manifest.files[0]
        assert entry.path == "test.txt"
        assert entry.size == len(content.encode("utf-8"))
        assert entry.mime_type  # non-empty
        assert entry.preview == content
