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
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from src.cli_client import OpenCodeClient, make_agent_client
from src.file_bus import FileBus, _md_to_html
from src.scanner import Manifest, WORKSPACE_DIR_NAME


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config(config_path: Path) -> Dict[str, Any]:
    with open(config_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load_prompt(prompt_path: Path, lang: str = "zh") -> str:
    """Load prompt from prompt_path; if lang=="en" try prompts/en/ variant first."""
    if lang == "en":
        en_path = prompt_path.parent.parent / "en" / prompt_path.name
        if en_path.exists():
            return en_path.read_text(encoding="utf-8")
    return prompt_path.read_text(encoding="utf-8")


def _render(template: str, **kwargs: Any) -> str:
    """Simple {key} substitution for prompt templates."""
    for key, value in kwargs.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def _available_specialist_names(prompts_dir: Path, lang: str = "zh") -> List[str]:
    """Return specialist names that have a matching prompt file, filtered by language.

    Chinese mode (zh): only names containing non-ASCII characters.
    English mode (en): only ASCII-only names (English prompt files).
    """
    specialists_dir = prompts_dir / "specialists"
    if not specialists_dir.exists():
        return []
    names = [p.stem for p in sorted(specialists_dir.glob("*.md")) if p.stem != "base"]
    if lang == "en":
        return [n for n in names if n.isascii()]
    # zh or any other: return only names with at least one non-ASCII character
    return [n for n in names if not n.isascii()]


def _extract_json(text: str) -> Dict[str, Any]:
    """
    Try to parse JSON from Agent output.

    The Agent may wrap its JSON in a Markdown code block — strip that first.
    Falls back to scanning for the first top-level ``{`` or ``[`` at a line
    boundary, which handles mini-agent ASCII-art banners that contain stray
    ``{…}`` sequences before the actual JSON payload.
    """
    # Strip ```json ... ``` or ``` ... ``` fences if present
    stripped = re.sub(r"```[a-zA-Z]*\n?", "", text).replace("```", "").strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        # Fallback: find the first line whose content starts with { or [
        # (skipping banner lines that begin with | or + but may contain braces)
        lines = stripped.splitlines()
        for i, line in enumerate(lines):
            if line.lstrip().startswith(("{", "[")):
                candidate = "\n".join(lines[i:]).strip()
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass
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
        lang: Optional[str] = None,
    ) -> None:
        self.bus = bus
        self.config_path = config_path
        self.prompts_dir = prompts_dir

        cfg = _load_config(config_path)
        oc_cfg = cfg.get("opencode", {})
        self.default_model: Optional[str] = oc_cfg.get("default_model") or None
        self.timeout: int = oc_cfg.get("timeout", 300)
        self.coordinator_timeout: int = oc_cfg.get("coordinator_timeout", self.timeout)
        self.synthesis_timeout: int = oc_cfg.get("synthesis_timeout", self.coordinator_timeout)
        self.coordinator_retries: int = oc_cfg.get("coordinator_retries", 1)
        self.registered_specialists: List[Dict[str, Any]] = cfg.get("specialists", [])
        # Language: explicit arg > config ui.language > default zh
        self.lang: str = lang or cfg.get("ui", {}).get("language", "zh")

        self.client = make_agent_client(
            cfg=oc_cfg,
            error_log_dir=bus.errors_dir,
            log_dir=bus.logs_dir,
        )

    # ------------------------------------------------------------------
    # Internal: retry wrapper
    # ------------------------------------------------------------------

    def _run_with_retry(
        self,
        fn: Callable[[], Any],
        step_name: str,
        retries: Optional[int] = None,
        retry_delay: float = 5.0,
    ) -> Any:
        """Call *fn()* up to *retries* times, retrying on AgentError.

        Each retry waits *retry_delay* seconds to give the API a moment to
        recover before the next opencode subprocess is launched.
        """
        from src.cli_client import AgentError
        max_attempts = (retries if retries is not None else self.coordinator_retries)
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                return fn()
            except AgentError as exc:
                last_exc = exc
                if attempt < max_attempts:
                    print(
                        f"[Coordinator] {step_name} attempt {attempt}/{max_attempts} failed "
                        f"({exc}). Retrying in {retry_delay}s…"
                    )
                    time.sleep(retry_delay)
                else:
                    print(
                        f"[Coordinator] {step_name} failed after {max_attempts} attempt(s): {exc}"
                    )
        raise last_exc  # re-raise the last exception after all retries exhausted

    # ------------------------------------------------------------------
    # Round 1: Index
    # ------------------------------------------------------------------

    def _build_file_texts(self) -> str:
        """Return a block containing the full extracted text of every file in the case.

        Reads .txt files from ``.mdt_workspace/context/{stem}_{ext}/{stem}.txt``
        (generated by ContextExtractor before AI runs). Falls back to the raw
        file text via scanner if the context file hasn't been created yet.
        """
        from src.scanner import _extract_text as scanner_extract
        parts: List[str] = []
        for fe in sorted(self.bus.case_dir.rglob("*")):
            if fe.is_dir():
                continue
            if WORKSPACE_DIR_NAME in fe.parts:
                continue
            if fe.suffix.lower() not in {".pdf", ".docx", ".xlsx", ".md", ".txt", ".csv", ".json", ".html", ".htm"}:
                continue
            # Prefer context-extracted .txt if available
            ctx_dir = self.bus.file_context_dir(fe)
            ctx_txt = ctx_dir / (fe.stem + ".txt")
            if ctx_txt.exists():
                text = ctx_txt.read_text(encoding="utf-8", errors="replace")
            else:
                text = scanner_extract(fe)
            if text.strip():
                rel = fe.relative_to(self.bus.case_dir)
                parts.append(f"=== {rel} ===\n{text}")
        return "\n\n".join(parts) if parts else "(no text content found)"

    def run_index(
        self,
        manifest: Manifest,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> Dict[str, Any]:
        """
        Ask the Coordinator Agent to classify files and assess completeness.

        Input:  Manifest (00_manifest.json) + full extracted file texts
        Output: index dict saved as 01_index.json
        """
        prompt_path = self.prompts_dir / "coordinator_index.md"
        base_prompt = _load_prompt(prompt_path, self.lang)

        manifest_json = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2)
        user_message = _render(
            base_prompt,
            case_dir=str(self.bus.case_dir),
            total_files=manifest.total_files,
            manifest_json=manifest_json,
        )

        print("[Coordinator] Round 1: Building file index …")
        sys_prompt = (
            "You are the MDT coordinator responsible for file classification."
            if self.lang == "en" else
            "你是 MDT 病例资料管理员，负责文件分类。"
        )
        raw = self._run_with_retry(
            lambda: self.client.run(
                agent_name="coordinator_index",
                system_prompt=sys_prompt,
                user_message=user_message,
                model=self.default_model,
                timeout=self.coordinator_timeout,
                read_allowed=False,
                on_event=on_event,
            ),
            step_name="run_index",
        )

        index = _extract_json(raw)
        self.bus.save_index(index)
        print(f"[Coordinator] Index saved → {self.bus.index_path}")
        return index

    # ------------------------------------------------------------------
    # Rounds 1+2 combined: Index + Dispatch (single opencode call)
    # ------------------------------------------------------------------

    def run_index_and_dispatch(
        self,
        manifest: Manifest,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> tuple:
        """
        Classify files AND decide specialist dispatch in a single opencode call.

        Saves 01_index.json and 02_dispatch.json separately for downstream
        compatibility.  Returns (index, dispatch).

        If both cached files already exist, skips the LLM call entirely.
        """
        if self.bus.index_path.exists() and self.bus.dispatch_path.exists():
            try:
                index = json.loads(self.bus.index_path.read_text(encoding="utf-8"))
                dispatch = json.loads(self.bus.dispatch_path.read_text(encoding="utf-8"))
                print(f"[Coordinator] Index+Dispatch loaded from cache → {self.bus.workspace_dir}")
                return index, dispatch
            except Exception:
                pass  # corrupted cache — fall through and regenerate

        prompt_path = self.prompts_dir / "coordinator_index_dispatch.md"
        base_prompt = _load_prompt(prompt_path, self.lang)

        manifest_json = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2)

        available_names = _available_specialist_names(self.prompts_dir, self.lang)
        registered_by_name = {s["name"]: s for s in self.registered_specialists}
        available_specialists = [
            registered_by_name.get(name, {"name": name})
            for name in available_names
        ]
        available_json = json.dumps(available_specialists, ensure_ascii=False, indent=2)

        user_message = _render(
            base_prompt,
            case_dir=str(self.bus.case_dir),
            total_files=manifest.total_files,
            manifest_json=manifest_json,
            available_specialists_json=available_json,
        )

        print("[Coordinator] Rounds 1+2: Classifying files and dispatching specialists …")
        sys_prompt = (
            "You are the MDT coordinator responsible for file classification and specialist dispatch."
            if self.lang == "en" else
            "你是 MDT 病例资料管理员兼主持人，负责文件分类与专科调度。"
        )
        raw = self._run_with_retry(
            lambda: self.client.run(
                agent_name="coordinator_index_dispatch",
                system_prompt=sys_prompt,
                user_message=user_message,
                model=self.default_model,
                timeout=self.coordinator_timeout,
                read_allowed=False,
                on_event=on_event,
            ),
            step_name="run_index_and_dispatch",
        )

        combined = _extract_json(raw)

        _INDEX_KEYS = {"file_classifications", "case_completeness", "summary"}
        _DISPATCH_KEYS = {"specialists_required", "notes"}
        index = {k: v for k, v in combined.items() if k in _INDEX_KEYS}
        dispatch = {k: v for k, v in combined.items() if k in _DISPATCH_KEYS}

        self.bus.save_index(index)
        self.bus.save_dispatch(dispatch)
        print(f"[Coordinator] Index+Dispatch saved → {self.bus.workspace_dir}")
        return index, dispatch

    # ------------------------------------------------------------------
    # Round 2: Dispatch
    # ------------------------------------------------------------------

    def run_dispatch(
        self,
        index: Dict[str, Any],
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> Dict[str, Any]:
        """
        Decide which specialists to involve, and which files each should read.

        Input:  index (01_index.json)
        Output: dispatch dict saved as 02_dispatch.json
        """
        # Skip if already generated (avoid redundant LLM call on re-runs)
        if self.bus.dispatch_path.exists():
            try:
                cached = json.loads(self.bus.dispatch_path.read_text(encoding="utf-8"))
                print(f"[Coordinator] Dispatch loaded from cache → {self.bus.dispatch_path}")
                return cached
            except Exception:
                pass  # corrupted cache — fall through and regenerate

        prompt_path = self.prompts_dir / "coordinator_dispatch.md"
        base_prompt = _load_prompt(prompt_path, self.lang)

        # Build the list of *available* specialists (those with prompt files),
        # filtered to the current language so English + Chinese names don't mix.
        available_names = _available_specialist_names(self.prompts_dir, self.lang)
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
        sys_prompt = (
            "You are the MDT chairperson deciding specialist team composition."
            if self.lang == "en" else
            "你是 MDT 主持人，负责决定专科会诊团队构成。"
        )
        raw = self._run_with_retry(
            lambda: self.client.run(
                agent_name="coordinator_dispatch",
                system_prompt=sys_prompt,
                user_message=user_message,
                model=self.default_model,
                timeout=self.coordinator_timeout,
                read_allowed=False,
                on_event=on_event,
            ),
            step_name="run_dispatch",
        )

        dispatch = _extract_json(raw)
        self.bus.save_dispatch(dispatch)
        print(f"[Coordinator] Dispatch saved → {self.bus.dispatch_path}")
        return dispatch

    # ------------------------------------------------------------------
    # Round 3: Synthesis
    # ------------------------------------------------------------------

    def run_synthesis(
        self,
        index: Dict[str, Any],
        opinions: Dict[str, str],
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> str:
        """
        Generate the final MDT report from all specialist opinions.

        Input:  index + opinions
        Output: HTML report saved as 05_mdt_report.html; HTML string returned.
        """
        prompt_path = self.prompts_dir / "coordinator_synthesis.md"
        base_prompt = _load_prompt(prompt_path, self.lang)

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
        sys_prompt = (
            "You are the MDT chairperson with 20 years of clinical experience."
            if self.lang == "en" else
            "你是 MDT 主持人，拥有20年临床主持经验。"
        )
        report_text = self._run_with_retry(
            lambda: self.client.run(
                agent_name="coordinator_synthesis",
                system_prompt=sys_prompt,
                user_message=user_message,
                model=self.default_model,
                timeout=self.synthesis_timeout,
                read_allowed=False,
                bash_allowed=False,
                on_event=on_event,
            ),
            step_name="run_synthesis",
        )

        if not report_text or not report_text.strip():
            raise ValueError(
                "Synthesis agent returned empty output. "
                "Check the audit log in logs/ for details."
            )

        self.bus.save_report(_md_to_html(report_text, title="MDT Report"))
        print(f"[Coordinator] Report saved → {self.bus.report_path}")
        return report_text
