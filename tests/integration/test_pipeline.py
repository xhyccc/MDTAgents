"""
Integration test for the full MDT pipeline.

This test exercises the complete flow (scanner → coordinator index →
coordinator dispatch → specialist pool → coordinator synthesis) by
replacing the actual ``opencode`` CLI with a lightweight fake implementation.

The fake binary is written to a temp directory and added to PATH for the
duration of the test.  It emits deterministic JSON / Markdown responses so
the pipeline can proceed end-to-end without any real AI calls.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src.file_bus import FileBus
from src.scanner import Scanner
from src.coordinator import Coordinator
from src.specialist_pool import SpecialistPool


# ---------------------------------------------------------------------------
# Fake opencode responses
# ---------------------------------------------------------------------------

# Round 1: coordinator_index output
INDEX_RESPONSE = json.dumps({
    "files_classified": [
        {"file": "入院记录.md", "category": "病历", "description": "入院记录"},
        {"file": "CT检查.md",  "category": "影像", "description": "CT报告"},
        {"file": "病理报告.md", "category": "病理", "description": "病理报告"},
        {"file": "血常规.md",  "category": "检验", "description": "血常规"},
    ],
    "completeness": "complete",
    "missing_items": [],
    "summary": "资料完整，可启动MDT会诊。",
}, ensure_ascii=False, indent=2)

# Round 2: coordinator_dispatch output
DISPATCH_RESPONSE = json.dumps({
    "rationale": "根据资料类型分配专科。",
    "specialists_required": [
        {"name": "影像科", "model": "test-model", "files_assigned": ["CT检查.md"]},
        {"name": "病理科", "model": "test-model", "files_assigned": ["病理报告.md"]},
    ],
}, ensure_ascii=False, indent=2)

# Round 3: each specialist opinion (returned as plain text)
SPECIALIST_OPINION_TEMPLATE = (
    "# {name}会诊意见\n\n"
    "## 一、资料概述\n阅读了相关资料。\n\n"
    "## 二、专科分析\n未见明显异常。\n\n"
    "## 三、初步结论\n建议随访。\n\n"
    "## 四、需要补充的资料\n暂无。\n"
)

# Round 4: synthesis output
SYNTHESIS_RESPONSE = (
    "# MDT 多学科会诊报告\n\n"
    "## 综合意见\n经影像科、病理科联合会诊，患者情况良好，建议保守治疗。\n"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_CONFIG = {
    "opencode": {
        "default_model": "test-model",
        "timeout": 60,
        "max_workers": 2,
    },
    "specialists": [
        {"name": "影像科", "model": "test-model"},
        {"name": "病理科", "model": "test-model"},
    ],
}


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Set up a minimal project layout inside tmp_path."""
    # config/
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "system.yaml").write_text(
        yaml.dump(MINIMAL_CONFIG, allow_unicode=True), encoding="utf-8"
    )

    # prompts/
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    for fname, content in [
        ("coordinator_index.md", "Classify the files: {case_dir} {total_files} {manifest_json}"),
        ("coordinator_dispatch.md", "Dispatch: {index_json} {available_specialists_json}"),
        ("coordinator_synthesis.md", "Synthesize: {index_json} {opinions_json}"),
    ]:
        (prompts_dir / fname).write_text(content, encoding="utf-8")

    spec_dir = prompts_dir / "specialists"
    spec_dir.mkdir()
    (spec_dir / "base.md").write_text("You are a specialist.", encoding="utf-8")
    (spec_dir / "影像科.md").write_text("You are an radiologist.", encoding="utf-8")
    (spec_dir / "病理科.md").write_text("You are a pathologist.", encoding="utf-8")

    # cases/demo_case/
    case_dir = tmp_path / "cases" / "demo_case"
    case_dir.mkdir(parents=True)
    (case_dir / "入院记录.md").write_text("患者：张三，60岁男性。", encoding="utf-8")
    (case_dir / "CT检查.md").write_text("CT所见：肺部未见异常。", encoding="utf-8")
    (case_dir / "病理报告.md").write_text("病理：良性。", encoding="utf-8")
    (case_dir / "血常规.md").write_text("血常规：正常。", encoding="utf-8")

    return tmp_path


@pytest.fixture
def case_dir(project_root: Path) -> Path:
    return project_root / "cases" / "demo_case"


# ---------------------------------------------------------------------------
# Helper: build a fake opencode script that echoes canned responses
# ---------------------------------------------------------------------------

def _make_fake_opencode(bin_dir: Path, responses: list[str]) -> Path:
    """
    Write a tiny shell script that outputs responses from a JSON array file,
    cycling through them on each invocation via a counter file.
    """
    responses_file = bin_dir / "_responses.json"
    responses_file.write_text(json.dumps(responses), encoding="utf-8")

    counter_file = bin_dir / "_counter.txt"
    counter_file.write_text("0", encoding="utf-8")

    # Python-based fake binary (portable across platforms)
    fake_script = bin_dir / "opencode"
    fake_script.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json, sys
            from pathlib import Path

            responses_file = Path({str(responses_file)!r})
            counter_file   = Path({str(counter_file)!r})

            responses = json.loads(responses_file.read_text())
            idx = int(counter_file.read_text().strip())
            response = responses[idx % len(responses)]
            counter_file.write_text(str(idx + 1))

            # Handle --version specially
            if "--version" in sys.argv:
                print("opencode 0.0.0-fake")
                sys.exit(0)

            print(response)
        """),
        encoding="utf-8",
    )
    fake_script.chmod(fake_script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake_script


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

class TestFullPipeline:
    """Run the complete scanner → index → dispatch → pool → synthesis pipeline."""

    def _run_pipeline(self, project_root: Path, case_dir: Path) -> FileBus:
        cfg_path = project_root / "config" / "system.yaml"
        prompts_dir = project_root / "prompts"

        bus = FileBus(case_dir)
        bus.init_workspace()

        # Round 0: scan
        scanner = Scanner()
        manifest = scanner.scan(case_dir)
        bus.save_manifest(manifest)

        # Prepare fake opencode responses in call order:
        #   1st call  → coordinator_index  → INDEX_RESPONSE
        #   2nd call  → coordinator_dispatch → DISPATCH_RESPONSE
        #   3rd call  → specialist 影像科  → opinion
        #   4th call  → specialist 病理科  → opinion
        #   5th call  → coordinator_synthesis → SYNTHESIS_RESPONSE
        opinions_text_ying = SPECIALIST_OPINION_TEMPLATE.format(name="影像科")
        opinions_text_bing = SPECIALIST_OPINION_TEMPLATE.format(name="病理科")
        responses = [
            INDEX_RESPONSE,
            DISPATCH_RESPONSE,
            opinions_text_ying,
            opinions_text_bing,
            SYNTHESIS_RESPONSE,
        ]

        bin_dir = project_root / "_fake_bin"
        bin_dir.mkdir(exist_ok=True)
        _make_fake_opencode(bin_dir, responses)
        new_path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")

        with patch.dict(os.environ, {"PATH": new_path}):
            coordinator = Coordinator(bus=bus, config_path=cfg_path, prompts_dir=prompts_dir)
            index = coordinator.run_index(manifest)
            dispatch = coordinator.run_dispatch(index)

            pool = SpecialistPool(bus=bus, config_path=cfg_path, prompts_dir=prompts_dir)
            opinions = pool.run_parallel(dispatch)

            coordinator.run_synthesis(index, opinions)

        return bus

    def test_pipeline_creates_all_workspace_artifacts(self, project_root: Path, case_dir: Path):
        bus = self._run_pipeline(project_root, case_dir)
        assert bus.manifest_path.exists(), "00_manifest.json missing"
        assert bus.index_path.exists(),    "01_index.json missing"
        assert bus.dispatch_path.exists(), "02_dispatch.json missing"
        assert bus.report_path.exists(),   "05_mdt_report.md missing"

    def test_pipeline_scanner_finds_all_case_files(self, project_root: Path, case_dir: Path):
        bus = self._run_pipeline(project_root, case_dir)
        manifest = bus.load_manifest()
        assert manifest["total_files"] == 4

    def test_pipeline_index_has_expected_structure(self, project_root: Path, case_dir: Path):
        bus = self._run_pipeline(project_root, case_dir)
        index = bus.load_index()
        assert "files_classified" in index
        assert index["completeness"] == "complete"

    def test_pipeline_dispatch_has_specialists(self, project_root: Path, case_dir: Path):
        bus = self._run_pipeline(project_root, case_dir)
        dispatch = bus.load_dispatch()
        assert "specialists_required" in dispatch
        assert len(dispatch["specialists_required"]) == 2

    def test_pipeline_specialist_opinions_saved(self, project_root: Path, case_dir: Path):
        bus = self._run_pipeline(project_root, case_dir)
        opinions = bus.load_opinions()
        assert len(opinions) == 2
        assert "影像科" in opinions
        assert "病理科" in opinions

    def test_pipeline_report_contains_content(self, project_root: Path, case_dir: Path):
        bus = self._run_pipeline(project_root, case_dir)
        report = bus.report_path.read_text(encoding="utf-8")
        assert len(report) > 0
        assert "MDT" in report


class TestPipelineScannerOnly:
    """Lightweight tests that don't need a fake opencode binary."""

    def test_scanner_excludes_workspace(self, case_dir: Path):
        bus = FileBus(case_dir)
        bus.init_workspace()
        # Put a file inside workspace — it should not appear in scan
        (bus.workspace_dir / "dummy.md").write_text("ignore me", encoding="utf-8")

        scanner = Scanner()
        manifest = scanner.scan(case_dir)
        paths = [f["path"] if isinstance(f, dict) else f.path for f in manifest.files]
        assert all(".mdt_workspace" not in p for p in paths)

    def test_scanner_manifest_saved_to_disk(self, case_dir: Path):
        bus = FileBus(case_dir)
        bus.init_workspace()
        scanner = Scanner()
        manifest = scanner.scan(case_dir)
        bus.save_manifest(manifest)
        assert bus.manifest_path.exists()
        loaded = bus.load_manifest()
        assert loaded["case_id"] == "demo_case"
