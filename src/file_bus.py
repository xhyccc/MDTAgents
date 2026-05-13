"""
file_bus.py — File-system message bus for the MDT workspace.

All inter-Agent communication happens through files under .mdt_workspace/.
This module manages that directory: creating it, writing artifacts, and
exposing typed read helpers.  No medical content is parsed here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from src.scanner import Manifest


WORKSPACE_DIR_NAME = ".mdt_workspace"


class FileBus:
    """
    Manages the .mdt_workspace/ directory within a case folder.

    Directory layout
    ----------------
    .mdt_workspace/
    ├── 00_manifest.json
    ├── 01_index.json
    ├── 02_dispatch.json
    ├── 03_opinions/
    │   ├── <specialist_name>.md
    │   └── ...
    ├── 04_debate.json          (optional)
    ├── 05_mdt_report.md
    └── errors/
        └── <agent_name>.log
    """

    def __init__(self, case_dir: Path) -> None:
        self.case_dir: Path = Path(case_dir).resolve()
        self.workspace_dir: Path = self.case_dir / WORKSPACE_DIR_NAME
        self.opinions_dir: Path = self.workspace_dir / "03_opinions"
        self.errors_dir: Path = self.workspace_dir / "errors"
        self.context_dir: Path = self.workspace_dir / "context"  # extracted text files for agents

        # Convenient path references
        self.manifest_path: Path = self.workspace_dir / "00_manifest.json"
        self.index_path: Path = self.workspace_dir / "01_index.json"
        self.dispatch_path: Path = self.workspace_dir / "02_dispatch.json"
        self.debate_path: Path = self.workspace_dir / "04_debate.json"
        self.report_path: Path = self.workspace_dir / "05_mdt_report.md"

    # ------------------------------------------------------------------
    # Workspace lifecycle
    # ------------------------------------------------------------------

    def init_workspace(self) -> None:
        """Create workspace directories (idempotent)."""
        for directory in (
            self.workspace_dir,
            self.opinions_dir,
            self.errors_dir,
            self.context_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Agent workspace helpers
    # ------------------------------------------------------------------

    def file_context_dir(self, file_path: Path) -> Path:
        """Return context subfolder for *file_path* inside context_dir.

        E.g. ``cases/x/报告.pdf`` → ``.mdt_workspace/context/报告_pdf/``
        """
        stem = file_path.stem
        ext = file_path.suffix.lstrip(".").lower() or "file"
        return self.context_dir / f"{stem}_{ext}"

    def agent_workspace_dir(self, name: str) -> Path:
        """Return per-agent workspace directory path (inside .mdt_workspace/)."""
        return self.workspace_dir / f"{name}_workspace"

    def build_agent_workspaces(self, dispatch: Dict[str, Any]) -> Dict[str, Path]:
        """Create per-agent workspace dirs populated with assigned files + context folders.

        For each specialist in *dispatch*, creates::

            .mdt_workspace/{name}_workspace/
            ├── original_file.pdf
            ├── original_file_pdf/
            │   ├── original_file.txt
            │   ├── page_001.png
            │   └── image_001.png
            └── …

        Returns
        -------
        dict
            ``{specialist_name: workspace_path}`` for every specialist.
        """
        import shutil
        workspaces: Dict[str, Path] = {}
        for spec in dispatch.get("specialists_required", []):
            name: str = spec["name"]
            ws = self.agent_workspace_dir(name)
            ws.mkdir(parents=True, exist_ok=True)
            for rel in spec.get("files_assigned", []):
                src = self.case_dir / rel
                if not src.exists():
                    continue
                # Copy original file (skip if already there)
                dst_file = ws / src.name
                if not dst_file.exists():
                    shutil.copy2(src, dst_file)
                # Copy context folder if it exists
                ctx_folder = self.file_context_dir(src)
                if ctx_folder.exists():
                    dst_ctx = ws / ctx_folder.name
                    if not dst_ctx.exists():
                        shutil.copytree(src=ctx_folder, dst=dst_ctx)
            workspaces[name] = ws
        return workspaces

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def save_manifest(self, manifest: Manifest) -> None:
        self._write_json(self.manifest_path, manifest.to_dict())

    def save_index(self, index: Dict[str, Any]) -> None:
        self._write_json(self.index_path, index)

    def save_dispatch(self, dispatch: Dict[str, Any]) -> None:
        self._write_json(self.dispatch_path, dispatch)

    def save_opinion(self, specialist_name: str, opinion_text: str) -> Path:
        """Write a specialist's opinion to 03_opinions/{name}.md."""
        opinion_path = self.opinions_dir / f"{specialist_name}.md"
        opinion_path.write_text(opinion_text, encoding="utf-8")
        return opinion_path

    def save_debate(self, debate: Dict[str, Any]) -> None:
        self._write_json(self.debate_path, debate)

    def save_report(self, report_text: str) -> None:
        self.report_path.write_text(report_text, encoding="utf-8")

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def load_manifest(self) -> Dict[str, Any]:
        return self._read_json(self.manifest_path)

    def load_index(self) -> Dict[str, Any]:
        return self._read_json(self.index_path)

    def load_dispatch(self) -> Dict[str, Any]:
        return self._read_json(self.dispatch_path)

    def load_opinions(self) -> Dict[str, str]:
        """Return {specialist_name: opinion_text} for all saved opinions."""
        opinions: Dict[str, str] = {}
        for opinion_file in sorted(self.opinions_dir.glob("*.md")):
            name = opinion_file.stem
            opinions[name] = opinion_file.read_text(encoding="utf-8")
        return opinions

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write_json(self, path: Path, data: Any) -> None:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _read_json(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))
