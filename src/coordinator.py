"""
coordinator.py — Three-round Coordinator engine.

Round 1 (Index):    File classification and completeness assessment.
Round 2 (Dispatch): Specialist team assembly from available prompts.
Round 3 (Synthesis): Consensus report from all specialist opinions.

All medical judgment is delegated to the Agent; Python only manages I/O.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.cli_client import OpenCodeClient
from src.file_bus import FileBus
from src.scanner import Manifest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config(config_path: Path) -> Dict[str, Any]:
    with open(config_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load_prompt(prompt_path: Path) -> str:
    return prompt_path.read_text(encoding="utf-8")


def _render(template: str, **kwargs: Any) -> str:
    """Simple {key} substitution for prompt templates."""
    for key, value in kwargs.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def _available_specialist_names(prompts_dir: Path) -> List[str]:
    """Return specialist names that have a matching prompt file."""
    specialists_dir = prompts_dir / "specialists"
    if not specialists_dir.exists():
        return []
    return [
        p.stem
        for p in sorted(specialists_dir.glob("*.md"))
        if p.stem != "base"
    ]


def _extract_json(text: str) -> Dict[str, Any]:
    """
    Try to parse JSON from Agent output.

    The Agent may wrap its JSON in a Markdown code block — strip that first.
    """
    # Strip ```json ... ``` or ``` ... ``` fences if present
    stripped = re.sub(r"```[a-zA-Z]*\n?", "", text).replace("```", "").strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        # Fallback: try to find the first { ... } block
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise ValueError(f"Could not parse JSON from Agent output:\n{text[:500]}")


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class Coordinator:
    """
    Drives the three-round MDT coordination workflow.

    Parameters
    ----------
    bus:
        FileBus instance for the current case.
    config_path:
        Path to config/system.yaml.
    prompts_dir:
        Root directory containing coordinator and specialist prompts.
    """

    def __init__(
        self,
        bus: FileBus,
        config_path: Path = Path("config/system.yaml"),
        prompts_dir: Path = Path("prompts"),
    ) -> None:
        self.bus = bus
        self.config_path = config_path
        self.prompts_dir = prompts_dir

        cfg = _load_config(config_path)
        oc_cfg = cfg.get("opencode", {})
        self.default_model: Optional[str] = oc_cfg.get("default_model") or None
        self.timeout: int = oc_cfg.get("timeout", 300)
        self.registered_specialists: List[Dict[str, Any]] = cfg.get("specialists", [])

        self.client = OpenCodeClient(
            error_log_dir=bus.errors_dir,
            default_model=self.default_model,
        )

    # ------------------------------------------------------------------
    # Round 1: Index
    # ------------------------------------------------------------------

    def run_index(self, manifest: Manifest) -> Dict[str, Any]:
        """
        Ask the Coordinator Agent to classify files and assess completeness.

        Input:  Manifest (00_manifest.json)
        Output: index dict saved as 01_index.json
        """
        prompt_path = self.prompts_dir / "coordinator_index.md"
        base_prompt = _load_prompt(prompt_path)

        manifest_json = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2)
        user_message = _render(
            base_prompt,
            case_dir=str(self.bus.case_dir),
            total_files=manifest.total_files,
            manifest_json=manifest_json,
        )

        print("[Coordinator] Round 1: Building file index …")
        raw = self.client.run(
            agent_name="coordinator_index",
            system_prompt="You are the MDT coordinator responsible for file classification.",
            user_message=user_message,
            model=self.default_model,
            timeout=self.timeout,
        )

        index = _extract_json(raw)
        self.bus.save_index(index)
        print(f"[Coordinator] Index saved → {self.bus.index_path}")
        return index

    # ------------------------------------------------------------------
    # Round 2: Dispatch
    # ------------------------------------------------------------------

    def run_dispatch(self, index: Dict[str, Any]) -> Dict[str, Any]:
        """
        Decide which specialists to involve, and which files each should read.

        Input:  index (01_index.json)
        Output: dispatch dict saved as 02_dispatch.json
        """
        prompt_path = self.prompts_dir / "coordinator_dispatch.md"
        base_prompt = _load_prompt(prompt_path)

        # Build the list of *available* specialists (those with prompt files)
        available_names = _available_specialist_names(self.prompts_dir)
        # Merge with registry to include model info
        registered_by_name = {s["name"]: s for s in self.registered_specialists}
        available_specialists = [
            registered_by_name.get(name, {"name": name})
            for name in available_names
        ]

        index_json = json.dumps(index, ensure_ascii=False, indent=2)
        available_json = json.dumps(available_specialists, ensure_ascii=False, indent=2)

        user_message = _render(
            base_prompt,
            index_json=index_json,
            available_specialists_json=available_json,
        )

        print("[Coordinator] Round 2: Dispatching specialists …")
        raw = self.client.run(
            agent_name="coordinator_dispatch",
            system_prompt="You are the MDT chairperson deciding specialist team composition.",
            user_message=user_message,
            model=self.default_model,
            timeout=self.timeout,
        )

        dispatch = _extract_json(raw)
        self.bus.save_dispatch(dispatch)
        print(f"[Coordinator] Dispatch saved → {self.bus.dispatch_path}")
        return dispatch

    # ------------------------------------------------------------------
    # Round 3: Synthesis
    # ------------------------------------------------------------------

    def run_synthesis(
        self, index: Dict[str, Any], opinions: Dict[str, str]
    ) -> str:
        """
        Generate the final MDT report from all specialist opinions.

        Input:  index + opinions
        Output: Markdown report saved as 05_mdt_report.md
        """
        prompt_path = self.prompts_dir / "coordinator_synthesis.md"
        base_prompt = _load_prompt(prompt_path)

        # Format opinions as a readable block
        opinions_parts = []
        for specialist, opinion in opinions.items():
            opinions_parts.append(f"## {specialist}\n\n{opinion}")
        opinions_block = "\n\n---\n\n".join(opinions_parts)

        opinions_json = json.dumps(opinions, ensure_ascii=False, indent=2)
        index_json = json.dumps(index, ensure_ascii=False, indent=2)

        user_message = _render(
            base_prompt,
            index_json=index_json,
            opinions_json=opinions_json,
        )

        print("[Coordinator] Round 3: Generating MDT report …")
        report_text = self.client.run(
            agent_name="coordinator_synthesis",
            system_prompt="You are the MDT chairperson with 20 years of clinical experience.",
            user_message=user_message,
            model=self.default_model,
            timeout=self.timeout,
        )

        self.bus.save_report(report_text)
        print(f"[Coordinator] Report saved → {self.bus.report_path}")
        return report_text
