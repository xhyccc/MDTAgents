"""
specialist_pool.py — Parallel specialist Agent pool.

Reads 02_dispatch.json, launches one OpenCode session per specialist using
ThreadPoolExecutor, and writes each opinion to 03_opinions/{name}.md.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.cli_client import AgentError, OpenCodeClient
from src.file_bus import FileBus


# ---------------------------------------------------------------------------
# User message template for specialist agents
# ---------------------------------------------------------------------------

SPECIALIST_USER_TEMPLATE = """\
你是 {specialist_name} 专家，请出具《{specialist_name}会诊意见》。

你的工作区
{workspace_path}

文件清单
{file_listing}

工作区说明
每份原始文件（PDF/Word等）都有一个同名的 <stem>_<ext>/ 展开文件夹，内含：
  · <stem>.txt       完整提取文本  ← 优先通过 read 工具读取此文件
  · page_NNN.png     逐页截图（若已生成）
  · image_NNN.png    内嵌图片（若已提取）
文本文件（.md/.txt/.csv/.json）可直接 read。

推理轮次（最多 {max_rounds} 轮）
你可以进行最多 {max_rounds} 轮推理。每轮可读取文件、审查证据或修正结论。
第 {max_rounds} 轮结束后，你必须输出最终会诊意见。若提前确定结论，可直接输出，无需耗尽所有轮次。

纪律
1. 必须基于你实际阅读到的资料内容做判断，不要编造
2. 如果资料不足以做出明确判断，请明确说明"资料不足：缺少xxx"
3. 不要越界给出其他专科的治疗建议
4. 输出格式：
   - {specialist_name}会诊意见
   - 一、资料概述（简述你读了什么）
   - 二、专科分析
   - 三、初步结论
   - 四、需要补充的资料（如有）
"""

SPECIALIST_USER_TEMPLATE_EN = """\
You are a {specialist_name} specialist. Please provide your consultation opinion.

Your workspace
{workspace_path}

File listing
{file_listing}

Workspace layout
Each original binary file (PDF/Word/etc.) has a sibling folder named <stem>_<ext>/ containing:
  · <stem>.txt       full extracted text  ← read this first via the read tool
  · page_NNN.png     per-page screenshots (if rendered)
  · image_NNN.png    embedded raster images (if extracted)
Plain-text files (.md/.txt/.csv/.json) can be read directly.

Reasoning rounds (max {max_rounds})
You may perform up to {max_rounds} reasoning rounds. Each round may read files, review evidence, or revise conclusions.
After round {max_rounds} you must output your final consultation opinion. If you reach a conclusion earlier, output it immediately.

Discipline
1. Base all judgments on the materials you have actually read — do not fabricate
2. If the data is insufficient for a clear judgment, explicitly state "Insufficient data: missing xxx"
3. Do not exceed your specialty's scope to make treatment recommendations for other specialties
4. Output format:
   - {specialist_name} Consultation Opinion
   - I. Data Overview (briefly describe what you read)
   - II. Specialty Analysis
   - III. Preliminary Conclusions
   - IV. Additional Data Required (if any)
"""


def _load_config(config_path: Path) -> Dict[str, Any]:
    with open(config_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class SpecialistPool:
    """
    Manages parallel execution of specialist Agent sessions.

    Each specialist:
    1. Gets a system prompt = base.md + {name}.md concatenated.
    2. Gets a user message from the template above.
    3. Has the assigned files passed as --file attachments.
    4. Writes its opinion to 03_opinions/{name}.md via FileBus.
    """

    def __init__(
        self,
        bus: FileBus,
        config_path: Path = Path("config/system.yaml"),
        prompts_dir: Path = Path("prompts"),
        lang: Optional[str] = None,
    ) -> None:
        self.bus = bus
        self.prompts_dir = prompts_dir

        cfg = _load_config(config_path)
        oc_cfg = cfg.get("opencode", {})
        self.default_model: Optional[str] = oc_cfg.get("default_model") or None
        self.timeout: int = oc_cfg.get("timeout", 300)
        self.specialist_timeout: int = oc_cfg.get("specialist_timeout", 1800)
        self.max_workers: int = oc_cfg.get("max_workers", 5)
        self.max_rounds: int = oc_cfg.get("max_rounds", 5)
        self.lang: str = lang or cfg.get("ui", {}).get("language", "zh")

        self.client = OpenCodeClient(
            error_log_dir=bus.errors_dir,
            default_model=self.default_model,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_parallel(self, dispatch: Dict[str, Any]) -> Dict[str, str]:
        """
        Execute all specialists from the dispatch plan in parallel.

        Parameters
        ----------
        dispatch:
            The parsed 02_dispatch.json dict containing ``specialists_required``.

        Returns
        -------
        dict
            ``{specialist_name: opinion_text}`` for every completed specialist.
        """
        specialists: List[Dict[str, Any]] = dispatch.get("specialists_required", [])
        if not specialists:
            print("[SpecialistPool] No specialists dispatched.")
            return {}

        opinions: Dict[str, str] = {}
        errors: Dict[str, str] = {}

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_name = {
                executor.submit(self._run_specialist, spec): spec["name"]
                for spec in specialists
            }
            for future in as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    opinion_text = future.result()
                    opinions[name] = opinion_text
                    print(f"[SpecialistPool] {name}: opinion received ✓")
                except AgentError as exc:
                    errors[name] = str(exc)
                    print(f"[SpecialistPool] {name}: FAILED — {exc}")
                except Exception as exc:  # noqa: BLE001
                    errors[name] = str(exc)
                    print(f"[SpecialistPool] {name}: unexpected error — {exc}")

        if errors:
            print(f"[SpecialistPool] {len(errors)} specialist(s) failed: {list(errors.keys())}")

        return opinions

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_specialist(self, spec: Dict[str, Any]) -> str:
        name: str = spec["name"]
        files_assigned: List[str] = spec.get("files_assigned", [])
        model: Optional[str] = self.default_model

        # Skip if opinion already written (allows partial reruns without re-calling LLM)
        existing_opinion_path = self.bus.opinions_dir / f"{name}.md"
        if existing_opinion_path.exists():
            print(f"[SpecialistPool] {name}: opinion loaded from cache ✓")
            return existing_opinion_path.read_text(encoding="utf-8")

        system_prompt = self._build_system_prompt(name)

        # Use pre-built agent workspace (populated after dispatch, before consult).
        # Fall back to building it on the fly if missing.
        workspace = self.bus.agent_workspace_dir(name)
        if not workspace.exists():
            print(f"[SpecialistPool] {name}: workspace not found, building now…")
            self.bus.build_agent_workspaces({"specialists_required": [spec]})

        user_message = self._build_user_message(name, workspace)

        # Collect image files from workspace for vision (--file attachments).
        # Include: original image files + embedded images from context subfolders.
        # Exclude: page screenshots (too many; text covers the same content).
        image_suffixes = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
        image_paths: List[Path] = []
        if workspace.exists():
            for item in sorted(workspace.iterdir()):
                if item.is_file() and item.suffix.lower() in image_suffixes:
                    image_paths.append(item)
                elif item.is_dir():
                    for sub in sorted(item.iterdir()):
                        if sub.is_file() and sub.name.startswith("image_") and sub.suffix.lower() in image_suffixes:
                            image_paths.append(sub)

        opinion_text = self.client.run(
            agent_name=f"specialist_{name}",
            system_prompt=system_prompt,
            user_message=user_message,
            file_paths=image_paths,
            model=model,
            timeout=self.specialist_timeout,
        )

        self.bus.save_opinion(name, opinion_text)
        return opinion_text

    def _build_system_prompt(self, specialist_name: str) -> str:
        """Concatenate base.md + {specialist_name}.md, preferring lang variant."""
        def _pick(prompt_path: Path) -> Optional[str]:
            if self.lang == "en":
                en_path = prompt_path.parent.parent / "en" / "specialists" / prompt_path.name
                if en_path.exists():
                    return en_path.read_text(encoding="utf-8")
            if prompt_path.exists():
                return prompt_path.read_text(encoding="utf-8")
            return None

        base_path = self.prompts_dir / "specialists" / "base.md"
        specialist_path = self.prompts_dir / "specialists" / f"{specialist_name}.md"

        parts: List[str] = []
        base_text = _pick(base_path)
        if base_text:
            parts.append(base_text)
        spec_text = _pick(specialist_path)
        if spec_text:
            parts.append(spec_text)
        elif not parts:
            parts.append(f"You are a specialist in {specialist_name}.")

        return "\n\n---\n\n".join(parts)

    def _build_user_message(self, specialist_name: str, workspace_path: Path) -> str:
        template = SPECIALIST_USER_TEMPLATE_EN if self.lang == "en" else SPECIALIST_USER_TEMPLATE

        # Build an indented file listing of the workspace for the agent.
        lines: List[str] = []
        if workspace_path.exists():
            for item in sorted(workspace_path.iterdir()):
                if item.is_file():
                    lines.append(f"  {item.name}")
                elif item.is_dir():
                    lines.append(f"  {item.name}/")
                    for sub in sorted(item.iterdir()):
                        lines.append(f"    └─ {sub.name}")

        return template.format(
            specialist_name=specialist_name,
            workspace_path=str(workspace_path),
            file_listing="\n".join(lines) if lines else "  (empty)",
            max_rounds=self.max_rounds,
        )

    def _write_context_files(self, name: str, file_paths: List[Path]) -> List[tuple]:
        """Legacy: superseded by ContextExtractor + FileBus.build_agent_workspaces.
        Kept for backward compatibility; returns empty list."""
        return []

    def _extract_file_contents(self, file_paths: List[Path]) -> Dict[str, str]:
        """Extract full text from each file, returning {filename: text}."""
        from src.scanner import _extract_text
        contents: Dict[str, str] = {}
        for p in file_paths:
            contents[p.name] = _extract_text(p)
        return contents

    def _resolve_file_paths(self, files_assigned: List[str]) -> List[Path]:
        """Resolve relative filenames to absolute paths within the case directory."""
        resolved: List[Path] = []
        for rel in files_assigned:
            abs_path = self.bus.case_dir / rel
            if abs_path.exists():
                resolved.append(abs_path)
            else:
                print(f"[SpecialistPool] Warning: assigned file not found: {rel}")
        return resolved
