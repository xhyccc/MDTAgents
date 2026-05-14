"""
file_bus.py — File-system message bus for the MDT workspace.

All inter-Agent communication happens through files under .mdt_workspace/.
This module manages that directory: creating it, writing artifacts, and
exposing typed read helpers.  No medical content is parsed here.
"""

from __future__ import annotations

import html as _html_lib
import json
import re
import ssl
import urllib.request
import base64
import zlib
from pathlib import Path
from typing import Any, Dict, Optional

from src.scanner import Manifest


WORKSPACE_DIR_NAME = ".mdt_workspace"

# ---------------------------------------------------------------------------
# Mermaid server-side rendering (Kroki.io public API → inline SVG)
# ---------------------------------------------------------------------------

def _render_mermaid_svg(diagram_code: str) -> Optional[str]:
    """Render a Mermaid diagram to an SVG string via the Kroki.io public API.

    Returns the SVG string on success, or None if the request fails.
    Diagram code must be raw Mermaid syntax (not HTML-escaped).
    """
    try:
        compressed = zlib.compress(diagram_code.encode("utf-8"), level=9)
        encoded = base64.urlsafe_b64encode(compressed).decode("ascii")
        url = f"https://kroki.io/mermaid/svg/{encoded}"
        # macOS Python ships without the root CA bundle; skip verification for
        # the Kroki public endpoint (read-only, no sensitive data).
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(
            url, headers={"User-Agent": "MDTAgents/1.0", "Accept": "image/svg+xml"}
        )
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return resp.read().decode("utf-8")
    except Exception:
        return None


def _md_to_html(md_text: str, title: str = "MDT Report") -> str:
    """Convert a Markdown string to a styled, self-contained HTML document.

    Mermaid fenced code blocks are pre-rendered server-side to inline SVG via
    the Kroki.io public API, so no JavaScript CDN is needed at display time.
    If Kroki is unreachable the diagram is emitted as a ``<pre class="mermaid">``
    block so the bundled Mermaid.js (included in the HTML ``<script>`` tag)
    can render it client-side.

    The pre-processing pass runs on the raw markdown text *before* python-markdown
    so that blocks without surrounding blank lines (a common LLM quirk) are still
    captured reliably.
    """

    # ── Step 1: Pre-process mermaid blocks in raw markdown text ──────────────
    # python-markdown's fenced_code extension requires blank lines around a fence;
    # LLMs often omit them.  We handle them here on the raw source instead.
    def _render_mermaid_block(m: re.Match) -> str:
        raw_code = m.group(1).strip()
        svg = _render_mermaid_svg(raw_code)
        if svg:
            return (
                "\n\n"
                '<div class="mermaid-rendered" style="'
                "background:#f8f9fa;border-radius:8px;padding:16px;"
                'margin:16px 0;overflow-x:auto;text-align:center;">'
                f"{svg}</div>"
                "\n\n"
            )
        # Kroki unreachable: emit a <pre class="mermaid"> for Mermaid.js CDN fallback.
        # HTML-escape the code so < / > in diagram labels don't break the page;
        # Mermaid.js reads textContent so it decodes the entities automatically.
        escaped = _html_lib.escape(raw_code)
        return (
            "\n\n"
            '<div class="mermaid-fallback" style="'
            "background:#fff8e1;border-left:4px solid #f39c12;"
            'border-radius:8px;padding:16px;margin:16px 0;">'
            '<p style="margin:0 0 8px;color:#856404;font-size:0.85em;">'
            "⚠ 图表正在由浏览器渲染（Mermaid.js）…</p>"
            f'<pre class="mermaid" style="margin:0;background:transparent;">'
            f"{escaped}</pre>"
            "</div>"
            "\n\n"
        )

    md_text = re.sub(
        r"```mermaid[ \t]*\r?\n(.*?)\r?\n[ \t]*```",
        _render_mermaid_block,
        md_text,
        flags=re.DOTALL,
    )

    # ── Step 2: Convert remaining markdown to HTML ────────────────────────────
    try:
        import markdown as _md

        body = _md.markdown(md_text, extensions=["tables", "fenced_code", "nl2br"])

        # Belt-and-suspenders: catch any mermaid blocks that fenced_code DID parse
        # (e.g. if a block had proper blank lines and wasn't caught above).
        def _replace_mermaid(m: re.Match) -> str:
            raw_code = _html_lib.unescape(m.group(1)).strip()
            svg = _render_mermaid_svg(raw_code)
            if svg:
                return (
                    '<div class="mermaid-rendered" style="'
                    'background:#f8f9fa;border-radius:8px;padding:16px;'
                    'margin:16px 0;overflow-x:auto;text-align:center;">'
                    f"{svg}</div>"
                )
            escaped = _html_lib.escape(raw_code)
            return (
                '<div class="mermaid-fallback" style="'
                'background:#fff8e1;border-left:4px solid #f39c12;'
                'border-radius:8px;padding:16px;margin:16px 0;">'
                '<p style="margin:0 0 8px;color:#856404;font-size:0.85em;">'
                "⚠ 图表正在由浏览器渲染（Mermaid.js）…</p>"
                f'<pre class="mermaid" style="margin:0;background:transparent;">'
                f"{escaped}</pre>"
                "</div>"
            )

        body = re.sub(
            r'<pre><code class="language-mermaid">(.*?)</code></pre>',
            _replace_mermaid,
            body,
            flags=re.DOTALL,
        )
    except ImportError:
        body = f"<pre>{_html_lib.escape(md_text)}</pre>"

    safe_title = _html_lib.escape(title)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>
  body {{
    font-family: 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', Arial, sans-serif;
    max-width: 960px; margin: 40px auto; padding: 0 24px;
    color: #2c3e50; line-height: 1.75; background: #fff;
  }}
  h1 {{ color: #1a5276; border-bottom: 3px solid #1a5276; padding-bottom: 10px; font-size: 1.8em; margin-top: 0.8em; }}
  h2 {{ color: #1f618d; border-bottom: 1px solid #aed6f1; padding-bottom: 6px; margin-top: 2em; }}
  h3 {{ color: #21618c; margin-top: 1.5em; }}
  h4 {{ color: #2874a6; }}
  table {{ border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 0.95em; }}
  th {{ background: #1a5276; color: #fff; padding: 10px 14px; text-align: left; font-weight: 600; }}
  td {{ border: 1px solid #d5d8dc; padding: 9px 14px; }}
  tr:nth-child(even) td {{ background: #eaf4fb; }}
  tr:hover td {{ background: #d6eaf8; transition: background 0.15s; }}
  code {{ background: #f0f3f4; padding: 2px 6px; border-radius: 4px; font-size: 0.88em; font-family: 'Consolas', 'Courier New', monospace; }}
  pre {{ background: #f0f3f4; padding: 16px; border-radius: 8px; overflow-x: auto; border-left: 4px solid #2874a6; }}
  pre code {{ background: none; padding: 0; }}
  blockquote {{ border-left: 4px solid #2874a6; margin: 16px 0; padding: 8px 16px; background: #eaf4fb; color: #555; }}
  hr {{ border: none; border-top: 1px solid #d5d8dc; margin: 24px 0; }}
  a {{ color: #2874a6; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .mermaid-rendered svg {{ max-width: 100%; height: auto; }}
  @media print {{
    body {{ max-width: 100%; margin: 0; padding: 8px; }}
  }}
</style>
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  mermaid.initialize({{ startOnLoad: true, theme: 'default' }});
</script>
</head>
<body>
{body}
</body>
</html>"""


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
    │   ├── <specialist_name>.md    ← raw markdown (used by synthesis)
    │   ├── <specialist_name>.html  ← styled HTML (used by UI)
    │   └── ...
    ├── 04_debate.json          (optional)
    ├── 05_mdt_report.html
    ├── errors/                 (stderr on failure)
    └── logs/                   (full audit log per LLM call)
        └── <agent_name>.log
    """

    def __init__(self, case_dir: Path) -> None:
        self.case_dir: Path = Path(case_dir).resolve()
        self.workspace_dir: Path = self.case_dir / WORKSPACE_DIR_NAME
        self.opinions_dir: Path = self.workspace_dir / "03_opinions"
        self.errors_dir: Path = self.workspace_dir / "errors"
        self.logs_dir: Path = self.workspace_dir / "logs"
        self.context_dir: Path = self.workspace_dir / "context"  # extracted text files for agents

        # Convenient path references
        self.manifest_path: Path = self.workspace_dir / "00_manifest.json"
        self.index_path: Path = self.workspace_dir / "01_index.json"
        self.dispatch_path: Path = self.workspace_dir / "02_dispatch.json"
        self.debate_path: Path = self.workspace_dir / "04_debate.json"
        self.report_path: Path = self.workspace_dir / "05_mdt_report.html"

    # ------------------------------------------------------------------
    # Workspace lifecycle
    # ------------------------------------------------------------------

    def init_workspace(self) -> None:
        """Create workspace directories (idempotent)."""
        for directory in (
            self.workspace_dir,
            self.opinions_dir,
            self.errors_dir,
            self.logs_dir,
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

    def save_opinion(self, specialist_name: str, opinion_html: str) -> Path:
        """Write a specialist's styled HTML opinion to 03_opinions/{name}.html."""
        opinion_path = self.opinions_dir / f"{specialist_name}.html"
        opinion_path.write_text(opinion_html, encoding="utf-8")
        return opinion_path

    def save_opinion_md(self, specialist_name: str, opinion_md: str) -> Path:
        """Write a specialist's raw markdown opinion to 03_opinions/{name}.md."""
        opinion_path = self.opinions_dir / f"{specialist_name}.md"
        opinion_path.write_text(opinion_md, encoding="utf-8")
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
        """Return {specialist_name: opinion_html} for all saved HTML opinions."""
        opinions: Dict[str, str] = {}
        for opinion_file in sorted(self.opinions_dir.glob("*.html")):
            name = opinion_file.stem
            opinions[name] = opinion_file.read_text(encoding="utf-8")
        return opinions

    def load_opinions_md(self) -> Dict[str, str]:
        """Return {specialist_name: opinion_markdown} for all saved markdown opinions."""
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
