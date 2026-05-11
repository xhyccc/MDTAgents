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
你的任务
你是 {specialist_name} 专家。请阅读下方分配给你的病例资料，出具《{specialist_name}会诊意见》。

病例资料清单（已传入你的上下文）
{files_json}

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
    ) -> None:
        self.bus = bus
        self.prompts_dir = prompts_dir

        cfg = _load_config(config_path)
        oc_cfg = cfg.get("opencode", {})
        self.default_model: Optional[str] = oc_cfg.get("default_model") or None
        self.timeout: int = oc_cfg.get("timeout", 300)
        self.max_workers: int = oc_cfg.get("max_workers", 5)

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
        model: Optional[str] = spec.get("model") or self.default_model

        system_prompt = self._build_system_prompt(name)
        user_message = self._build_user_message(name, files_assigned)
        file_paths = self._resolve_file_paths(files_assigned)

        opinion_text = self.client.run(
            agent_name=f"specialist_{name}",
            system_prompt=system_prompt,
            user_message=user_message,
            file_paths=file_paths,
            model=model,
            timeout=self.timeout,
        )

        self.bus.save_opinion(name, opinion_text)
        return opinion_text

    def _build_system_prompt(self, specialist_name: str) -> str:
        """Concatenate base.md + {specialist_name}.md."""
        base_path = self.prompts_dir / "specialists" / "base.md"
        specialist_path = self.prompts_dir / "specialists" / f"{specialist_name}.md"

        parts: List[str] = []
        if base_path.exists():
            parts.append(base_path.read_text(encoding="utf-8"))
        if specialist_path.exists():
            parts.append(specialist_path.read_text(encoding="utf-8"))
        elif not parts:
            # Fallback: generic prompt
            parts.append(f"You are a specialist in {specialist_name}.")

        return "\n\n---\n\n".join(parts)

    def _build_user_message(self, specialist_name: str, files_assigned: List[str]) -> str:
        files_json = json.dumps(files_assigned, ensure_ascii=False, indent=2)
        return SPECIALIST_USER_TEMPLATE.format(
            specialist_name=specialist_name,
            files_json=files_json,
        )

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
