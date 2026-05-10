#!/usr/bin/env python3
"""
MDT-Orchestrator entry point.

Usage:
    python -m src.main cases/demo_case
    python src/main.py cases/demo_case
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m src.main <case_folder>")
        sys.exit(1)

    case_dir = Path(sys.argv[1]).resolve()
    if not case_dir.exists():
        raise FileNotFoundError(f"病例文件夹不存在: {case_dir}")

    # Determine project root (parent of src/)
    project_root = Path(__file__).resolve().parent.parent
    config_path = project_root / "config" / "system.yaml"
    prompts_dir = project_root / "prompts"

    # Lazy imports so the module is importable without all deps installed
    from src.file_bus import FileBus
    from src.scanner import Scanner
    from src.coordinator import Coordinator
    from src.specialist_pool import SpecialistPool

    # ── Round 0: Initialise workspace & scan ──────────────────────────
    bus = FileBus(case_dir)
    bus.init_workspace()

    print(f"[Main] Scanning case folder: {case_dir}")
    scanner = Scanner()
    manifest = scanner.scan(case_dir)
    bus.save_manifest(manifest)
    print(f"[Main] Found {manifest.total_files} file(s). Manifest saved.")

    # ── Rounds 1–2: Coordinator index + dispatch ───────────────────────
    coordinator = Coordinator(bus, config_path=config_path, prompts_dir=prompts_dir)
    index = coordinator.run_index(manifest)
    dispatch = coordinator.run_dispatch(index)

    # ── Round 3: Parallel specialist consultation ──────────────────────
    pool = SpecialistPool(bus, config_path=config_path, prompts_dir=prompts_dir)
    opinions = pool.run_parallel(dispatch)
    print(f"[Main] Received {len(opinions)} specialist opinion(s).")

    # ── Round 4: Synthesis ─────────────────────────────────────────────
    coordinator.run_synthesis(index, opinions)

    print()
    print("✅ MDT 完成。")
    print(f"   报告：{bus.report_path}")
    print(f"   工作区：{bus.workspace_dir}")


if __name__ == "__main__":
    main()
