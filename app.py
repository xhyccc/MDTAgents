"""
app.py — MDT-Orchestrator Web UI (Streamlit)

Tabs:
  🏥 Run MDT   — step-by-step pipeline visualization, multimodal support
  🔍 Debug     — browse workspace intermediate files
  ⚙️ Admin     — view / edit system config, manage specialists
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st
import yaml

# ── Project root ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
CASES_DIR = PROJECT_ROOT / "cases"
WORKSPACE_DIR_NAME = ".mdt_workspace"

# Workspace artifact filenames — all hardcoded constants, never user-controlled
_WS_MANIFEST = "00_manifest.json"
_WS_INDEX    = "01_index.json"
_WS_DISPATCH = "02_dispatch.json"
_WS_OPINIONS = "03_opinions"
_WS_DEBATE   = "04_debate.json"
_WS_REPORT   = "05_mdt_report.md"
_WS_ERRORS   = "errors"

# ── i18n ─────────────────────────────────────────────────────────────────────
T: Dict[str, Dict[str, str]] = {
    "app_title":        {"zh": "🏥 MDT-Orchestrator", "en": "🏥 MDT-Orchestrator"},
    "app_caption":      {"zh": "多学科会诊 AI 平台", "en": "Multidisciplinary Team AI Platform"},
    "lang_label":       {"zh": "界面语言 / Language", "en": "界面语言 / Language"},
    "config_label":     {"zh": "当前配置", "en": "Current Config"},
    "model_label":      {"zh": "活跃模型", "en": "Active Model"},
    "timeout_label":    {"zh": "超时", "en": "Timeout"},
    "workers_label":    {"zh": "最大并发", "en": "Max Workers"},
    "oc_ok":            {"zh": "✅ opencode 已安装", "en": "✅ opencode installed"},
    "oc_fail":          {"zh": "❌ opencode 未找到", "en": "❌ opencode not found"},
    "tab_run":          {"zh": "🏥 运行会诊", "en": "🏥 Run MDT"},
    "tab_debug":        {"zh": "🔍 调试", "en": "🔍 Debug"},
    "tab_admin":        {"zh": "⚙️ 管理", "en": "⚙️ Admin"},
    "run_header":       {"zh": "运行 MDT 会诊", "en": "Run MDT Consultation"},
    "input_mode":       {"zh": "输入方式", "en": "Input mode"},
    "mode_folder":      {"zh": "📁 选择病例文件夹", "en": "📁 Select case folder"},
    "mode_upload":      {"zh": "📤 上传病例文件", "en": "📤 Upload case files"},
    "select_case":      {"zh": "选择病例文件夹", "en": "Select case folder"},
    "upload_hint":      {"zh": "上传病例文件（支持 .md .txt .pdf .docx .xlsx .csv .json .html .png .jpg）",
                         "en": "Upload case files (.md .txt .pdf .docx .xlsx .csv .json .html .png .jpg)"},
    "btn_run":          {"zh": "▶ 开始会诊", "en": "▶ Start Consultation"},
    "btn_clear":        {"zh": "🗑 清除上次结果", "en": "🗑 Clear Last Results"},
    "pipeline_title":   {"zh": "📊 会诊进度", "en": "📊 Pipeline Progress"},
    "step_scan":        {"zh": "📁 文件扫描", "en": "📁 File Scan"},
    "step_index":       {"zh": "🔍 AI 分类", "en": "🔍 AI Index"},
    "step_dispatch":    {"zh": "📋 专科调度", "en": "📋 Dispatch"},
    "step_consult":     {"zh": "👥 专科会诊", "en": "👥 Consultation"},
    "step_report":      {"zh": "📝 综合报告", "en": "📝 Report"},
    "report_title":     {"zh": "📋 MDT 最终报告", "en": "📋 MDT Final Report"},
    "dl_md":            {"zh": "⬇ 下载报告 (.md)", "en": "⬇ Download Report (.md)"},
    "dl_html":          {"zh": "⬇ 下载报告 (.html)", "en": "⬇ Download Report (.html)"},
    "dl_pdf":           {"zh": "⬇ 下载报告 (.pdf)", "en": "⬇ Download Report (.pdf)"},
    "media_gallery":    {"zh": "🖼 病例影像文件", "en": "🖼 Case Media Files"},
    "debug_header":     {"zh": "Debug — 工作区文件浏览", "en": "Debug — Workspace Browser"},
    "admin_header":     {"zh": "Admin — 系统配置管理", "en": "Admin — System Configuration"},
    "save_config":      {"zh": "💾 保存配置", "en": "💾 Save Configuration"},
    "agent_vis_title":  {"zh": "🤝 多智能体协作可视化", "en": "🤝 Multi-Agent Collaboration"},
}


def _(key: str, lg: str) -> str:
    return T.get(key, {}).get(lg, T.get(key, {}).get("zh", key))


# ──────────────────────────────────────────────────────────────────────────────
# Page config (must be first Streamlit call)
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="MDT-Orchestrator",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _load_config() -> Dict[str, Any]:
    cfg_path = PROJECT_ROOT / "config" / "system.yaml"
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    return {}


def _save_config(data: Dict[str, Any]) -> None:
    cfg_path = PROJECT_ROOT / "config" / "system.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, allow_unicode=True, default_flow_style=False)


def _discover_case_dirs() -> List[Tuple[str, Path]]:
    if not CASES_DIR.is_dir():
        return []
    return [
        (sub.name, sub.resolve())
        for sub in sorted(CASES_DIR.iterdir())
        if sub.is_dir()
    ]


def _workspace_dir(case_dir: Path) -> Path:
    return case_dir / WORKSPACE_DIR_NAME


def _read_workspace_json(ws_dir: Path, filename: str) -> Optional[Any]:
    path = ws_dir / filename
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_workspace_text(ws_dir: Path, filename: str) -> Optional[str]:
    path = ws_dir / filename
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _opencode_installed() -> bool:
    try:
        result = subprocess.run(
            ["opencode", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _get_opencode_model() -> str:
    """Read the active model from opencode's config file."""
    config_candidates = [
        Path.home() / ".config" / "opencode" / "config.json",
        Path.home() / "Library" / "Application Support" / "opencode" / "config.json",
        Path.home() / ".opencode" / "config.json",
    ]
    for p in config_candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                for key in ("model", "default_model", "defaultModel"):
                    if key in data and data[key]:
                        return str(data[key])
                for v in data.values():
                    if isinstance(v, dict):
                        for key in ("model", "default_model"):
                            if key in v and v[key]:
                                return str(v[key])
            except Exception:
                pass
    cfg = _load_config()
    m = cfg.get("opencode", {}).get("default_model")
    if m:
        return m
    return "opencode built-in default"


def _report_to_html(report_md: str) -> str:
    """Convert markdown report to a styled HTML document."""
    try:
        import markdown as md_lib
        body = md_lib.markdown(report_md, extensions=["tables", "nl2br", "fenced_code"])
    except ImportError:
        import html
        body = f"<pre>{html.escape(report_md)}</pre>"
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>MDT Report</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 900px;
         margin: 40px auto; padding: 0 20px; color: #333; line-height: 1.7; }}
  h1 {{ color: #1a5276; border-bottom: 2px solid #1a5276; padding-bottom: 8px; }}
  h2 {{ color: #2874a6; border-bottom: 1px solid #aed6f1; padding-bottom: 4px; }}
  h3 {{ color: #1f618d; }}
  table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
  th, td {{ border: 1px solid #ccc; padding: 8px 12px; text-align: left; }}
  th {{ background: #d6eaf8; font-weight: 600; }}
  tr:nth-child(even) {{ background: #f8f9fa; }}
  code {{ background: #f0f0f0; padding: 2px 5px; border-radius: 3px; font-size: 0.9em; }}
  pre {{ background: #f0f0f0; padding: 16px; border-radius: 6px; overflow-x: auto; }}
</style>
</head>
<body>
{body}
</body>
</html>"""


def _report_to_pdf(report_md: str) -> Optional[bytes]:
    """Returns PDF bytes, or None if no PDF library is available."""
    html = _report_to_html(report_md)
    try:
        import io
        from xhtml2pdf import pisa  # type: ignore
        buf = io.BytesIO()
        result = pisa.CreatePDF(html, dest=buf)
        if not result.err:
            return buf.getvalue()
    except Exception:
        pass
    try:
        import weasyprint  # type: ignore
        return weasyprint.HTML(string=html).write_pdf()
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Language state
# ──────────────────────────────────────────────────────────────────────────────
if "lang" not in st.session_state:
    st.session_state.lang = _load_config().get("ui", {}).get("language", "zh")

lang: str = st.session_state.lang


# ──────────────────────────────────────────────────────────────────────────────
# Sidebar
# ──────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title(_("app_title", lang))
    st.caption(_("app_caption", lang))

    # Language toggle
    lang_options = ["🇨🇳 中文", "🇬🇧 English"]
    lang_idx = 0 if lang == "zh" else 1
    chosen = st.radio(_("lang_label", lang), lang_options, index=lang_idx, horizontal=True)
    new_lang = "zh" if chosen.startswith("🇨🇳") else "en"
    if new_lang != lang:
        st.session_state.lang = new_lang
        _cfg = _load_config()
        _cfg.setdefault("ui", {})["language"] = new_lang
        _save_config(_cfg)
        st.rerun()

    st.divider()

    oc_ok = _opencode_installed()
    if oc_ok:
        st.success(_("oc_ok", lang))
        active_model = _get_opencode_model()
        st.markdown(f"**{_('model_label', lang)}**")
        st.code(active_model, language=None)
    else:
        st.error(_("oc_fail", lang))
        st.caption("`bash scripts/setup.sh`")

    cfg_s = _load_config()
    oc_cfg_s = cfg_s.get("opencode", {})
    st.markdown(f"**{_('config_label', lang)}**")
    st.code(
        f"{_('timeout_label', lang)}: {oc_cfg_s.get('timeout', 'N/A')}s\n"
        f"{_('workers_label', lang)}: {oc_cfg_s.get('max_workers', 'N/A')}",
        language=None,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Pipeline visualization helpers
# ──────────────────────────────────────────────────────────────────────────────

def _render_pipeline_header(current_step: int, lg: str) -> None:
    steps = [
        _("step_scan",     lg),
        _("step_index",    lg),
        _("step_dispatch", lg),
        _("step_consult",  lg),
        _("step_report",   lg),
    ]
    cols = st.columns(len(steps))
    for i, (col, label) in enumerate(zip(cols, steps)):
        if i < current_step:
            col.success(label)
        elif i == current_step:
            col.info(f"⏳ {label}")
        else:
            col.empty()


def _render_agents_diagram(dispatch: Dict[str, Any], opinions: Dict[str, str], lg: str) -> None:
    st.markdown(f"### {_('agent_vis_title', lg)}")
    specs = dispatch.get("specialists_required", [])
    notes = dispatch.get("notes", [])

    spec_cards_html = ""
    for sp in specs:
        name = sp.get("name", "?")
        files = ", ".join(sp.get("files_assigned", []))
        done = name in opinions
        border = "#27ae60" if done else "#e67e22"
        icon = "✅" if done else "⏳"
        spec_cards_html += f"""
        <div style="border:2px solid {border}; border-radius:8px; padding:10px 14px;
                    background:{'#eafaf1' if done else '#fef9e7'}; margin:4px 0; min-width:180px;">
          <div style="font-weight:700; font-size:15px;">{icon} {name}</div>
          <div style="font-size:12px; color:#555; margin-top:4px;">📄 {files}</div>
        </div>"""

    diagram_html = f"""
    <div style="display:flex; align-items:center; flex-wrap:wrap; gap:12px; padding:16px;
                background:#f8f9fa; border-radius:10px; border:1px solid #dee2e6; margin-bottom:16px;">
      <div style="border:2px solid #2980b9; border-radius:8px; padding:10px 16px;
                  background:#d6eaf8; text-align:center; min-width:120px;">
        <div style="font-size:20px;">🧠</div>
        <div style="font-weight:700;">Coordinator</div>
        <div style="font-size:11px; color:#555;">Index + Dispatch</div>
      </div>
      <div style="font-size:28px; color:#888;">→</div>
      <div style="display:flex; flex-direction:column; gap:4px;">
        {spec_cards_html}
      </div>
      <div style="font-size:28px; color:#888;">→</div>
      <div style="border:2px solid #8e44ad; border-radius:8px; padding:10px 16px;
                  background:#f5eef8; text-align:center; min-width:120px;">
        <div style="font-size:20px;">📝</div>
        <div style="font-weight:700;">Synthesis</div>
        <div style="font-size:11px; color:#555;">MDT Report</div>
      </div>
    </div>"""
    st.html(diagram_html)

    if notes:
        label = "Dispatch Notes" if lg == "en" else "调度备注"
        with st.expander(f"📌 {label}"):
            for note in notes:
                st.write(f"• {note}")


def _display_report_with_media(report_text: str, case_dir: Path, lg: str) -> None:
    st.markdown(report_text)
    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    images = [
        f for f in sorted(case_dir.iterdir())
        if f.is_file() and f.suffix.lower() in image_exts
    ]
    if images:
        st.divider()
        st.subheader(_("media_gallery", lg))
        cols = st.columns(min(len(images), 3))
        for i, img_path in enumerate(images):
            with cols[i % 3]:
                st.image(str(img_path), caption=img_path.name, use_container_width=True)


def _run_pipeline_inline(case_dir: Path, lg: str) -> None:
    """Run full MDT pipeline in-process with step-by-step UI."""
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.file_bus import FileBus
    from src.scanner import Scanner
    from src.coordinator import Coordinator
    from src.specialist_pool import SpecialistPool
    from src.context_extractor import ContextExtractor

    config_path = PROJECT_ROOT / "config" / "system.yaml"
    prompts_dir = PROJECT_ROOT / "prompts"

    pipeline_ph = st.empty()

    # ── Init workspace ────────────────────────────────────────────────────
    bus = FileBus(case_dir)
    bus.init_workspace()

    with pipeline_ph.container():
        _render_pipeline_header(0, lg)

    # ── Step 1: Scan ──────────────────────────────────────────────────────
    scan_label = "📁 Scanning case files…" if lg == "en" else "📁 正在扫描病例文件…"
    with st.status(scan_label, expanded=True) as scan_status:
        try:
            scanner = Scanner()
            manifest = scanner.scan(case_dir)
            bus.save_manifest(manifest)
            st.write(f"{'Found' if lg == 'en' else '发现'} {manifest.total_files} {'file(s)' if lg == 'en' else '个文件'}")
            for fe in manifest.files:
                icon = "🖼" if fe.mime_type.startswith("image") else "📄"
                st.write(f"{icon} `{fe.path}` — {fe.mime_type} ({fe.size:,} bytes)")
            scan_status.update(
                label=f"✅ {_('step_scan', lg)} " + ("complete" if lg == "en" else "完成"),
                state="complete",
            )
        except Exception as exc:
            scan_status.update(label=f"❌ Scan failed: {exc}", state="error")
            return

    # ── Step 1b: Context extraction (deterministic) ───────────────────
    ctx_label = "🗂 Extracting file contexts…" if lg == "en" else "🗂 正在预提取文件内容…"
    with st.status(ctx_label, expanded=True) as ctx_status:
        try:
            file_paths = [case_dir / fe.path for fe in manifest.files]
            extractor = ContextExtractor(bus.context_dir)
            extractor.extract_all(file_paths, progress_cb=st.write)
            ctx_status.update(
                label=("✅ Context extraction complete" if lg == "en" else "✅ 文件预提取完成"),
                state="complete",
            )
        except Exception as exc:
            ctx_status.update(label=f"❌ Extraction failed: {exc}", state="error")
            return

    with pipeline_ph.container():
        _render_pipeline_header(1, lg)

    # ── Step 2: Index ─────────────────────────────────────────────────────
    idx_label = "🔍 AI file classification…" if lg == "en" else "🔍 AI 正在分类文件…"
    coordinator = Coordinator(bus, config_path=config_path, prompts_dir=prompts_dir, lang=lg)
    with st.status(idx_label, expanded=True) as idx_status:
        try:
            index = coordinator.run_index(manifest)
            for fc in index.get("file_classifications", []):
                st.write(f"• `{fc['path']}` → **{fc['category']}** ({fc.get('confidence', 0):.0%})")
            idx_status.update(
                label=f"✅ {_('step_index', lg)} " + ("complete" if lg == "en" else "完成"),
                state="complete",
            )
        except Exception as exc:
            idx_status.update(label=f"❌ Index failed: {exc}", state="error")
            return

    with pipeline_ph.container():
        _render_pipeline_header(2, lg)

    # ── Step 3: Dispatch ──────────────────────────────────────────────────
    disp_label = "📋 Planning specialist dispatch…" if lg == "en" else "📋 正在规划专科调度…"
    with st.status(disp_label, expanded=True) as disp_status:
        try:
            dispatch = coordinator.run_dispatch(index)
            specs = dispatch.get("specialists_required", [])
            st.write(f"{'Dispatching' if lg == 'en' else '调度'} {len(specs)} {'specialist(s)' if lg == 'en' else '个专科'}:")
            for sp in specs:
                files_str = ", ".join(sp.get("files_assigned", []))
                st.write(f"• **{sp['name']}** ← `{files_str}`")
                st.caption(f"  {sp.get('reason', '')}")
            disp_status.update(
                label=f"✅ {_('step_dispatch', lg)} ({len(specs)}) " + ("complete" if lg == "en" else "完成"),
                state="complete",
            )
        except Exception as exc:
            disp_status.update(label=f"❌ Dispatch failed: {exc}", state="error")
            return

    _render_agents_diagram(dispatch, {}, lg)

    # ── Step 3b: Build agent workspaces (deterministic) ───────────────
    ws_label = "📁 Building agent workspaces…" if lg == "en" else "📁 正在构建专科工作区…"
    with st.status(ws_label, expanded=True) as ws_status:
        try:
            workspaces = bus.build_agent_workspaces(dispatch)
            for ws_name, ws_path in workspaces.items():
                files_in_ws = [p.name for p in ws_path.iterdir() if p.is_file()]
                dirs_in_ws = [p.name + "/" for p in ws_path.iterdir() if p.is_dir()]
                st.write(f"• **{ws_name}**: `{ws_path.name}/` — {len(files_in_ws)} file(s), {len(dirs_in_ws)} context dir(s)")
            ws_status.update(
                label=("✅ Agent workspaces ready" if lg == "en" else f"✅ 专科工作区已就绪 ({len(workspaces)} 个)"),
                state="complete",
            )
        except Exception as exc:
            ws_status.update(label=f"❌ Workspace build failed: {exc}", state="error")
            return

    with pipeline_ph.container():
        _render_pipeline_header(3, lg)

    # ── Step 4: Parallel consultation ─────────────────────────────────────
    consult_label = "👥 Running parallel specialist consultations…" if lg == "en" else "👥 并行专科会诊进行中…"
    with st.status(consult_label, expanded=True) as consult_status:
        try:
            pool = SpecialistPool(bus, config_path=config_path, prompts_dir=prompts_dir, lang=lg)
            opinions = pool.run_parallel(dispatch)
            st.write(f"{len(opinions)} {'specialist opinion(s) received' if lg == 'en' else '份专科意见已收到'}")
            for name, opinion in opinions.items():
                with st.expander(f"📋 {name}"):
                    st.markdown(opinion[:600] + ("\n\n*…(truncated)*" if len(opinion) > 600 else ""))
            consult_status.update(
                label=f"✅ {_('step_consult', lg)} ({len(opinions)}) " + ("complete" if lg == "en" else "完成"),
                state="complete",
            )
        except Exception as exc:
            consult_status.update(label=f"❌ Consultation failed: {exc}", state="error")
            return

    _render_agents_diagram(dispatch, opinions, lg)

    with pipeline_ph.container():
        _render_pipeline_header(4, lg)

    # ── Step 5: Synthesis ─────────────────────────────────────────────────
    synth_label = "📝 Generating MDT report…" if lg == "en" else "📝 正在生成 MDT 综合报告…"
    with st.status(synth_label, expanded=True) as synth_status:
        try:
            report_text = coordinator.run_synthesis(index, opinions)
            synth_status.update(
                label=f"✅ {_('step_report', lg)} " + ("complete" if lg == "en" else "完成"),
                state="complete",
            )
        except Exception as exc:
            synth_status.update(label=f"❌ Synthesis failed: {exc}", state="error")
            return

    with pipeline_ph.container():
        _render_pipeline_header(5, lg)

    st.balloons()
    st.divider()

    st.subheader(_("report_title", lg))
    _display_report_with_media(report_text, case_dir, lg)

    st.divider()
    col_md, col_html, col_pdf = st.columns(3)
    col_md.download_button(
        _("dl_md", lg),
        data=report_text.encode("utf-8"),
        file_name="mdt_report.md",
        mime="text/markdown",
    )
    col_html.download_button(
        _("dl_html", lg),
        data=_report_to_html(report_text).encode("utf-8"),
        file_name="mdt_report.html",
        mime="text/html",
    )
    pdf_bytes = _report_to_pdf(report_text)
    if pdf_bytes:
        col_pdf.download_button(
            _("dl_pdf", lg),
            data=pdf_bytes,
            file_name="mdt_report.pdf",
            mime="application/pdf",
        )
    else:
        col_pdf.info("PDF: install xhtml2pdf or weasyprint" if lg == "en" else "PDF: 安装 xhtml2pdf 或 weasyprint")


# ──────────────────────────────────────────────────────────────────────────────
# Tabs
# ──────────────────────────────────────────────────────────────────────────────
tab_run, tab_debug, tab_admin = st.tabs([
    _("tab_run",   lang),
    _("tab_debug", lang),
    _("tab_admin", lang),
])


# ══════════════════════════════════════════════════════════════════════════════
# Tab 1: Run MDT
# ══════════════════════════════════════════════════════════════════════════════
with tab_run:
    st.header(_("run_header", lang))

    input_mode = st.radio(
        _("input_mode", lang),
        [_("mode_folder", lang), _("mode_upload", lang)],
        horizontal=True,
    )

    case_dir: Optional[Path] = None

    if input_mode == _("mode_folder", lang):
        discovered = _discover_case_dirs()
        if discovered:
            display_names = [name for name, _ in discovered]
            path_map = {name: path for name, path in discovered}
            selected_name = st.selectbox(_("select_case", lang), display_names)
            if selected_name:
                case_dir = path_map[selected_name]
                files = [
                    f.name for f in case_dir.iterdir()
                    if f.is_file() and not f.name.startswith(".")
                ]
                st.success(f"✅ `{selected_name}` — {len(files)} " + ("file(s)" if lang == "en" else "个文件"))
                if files:
                    with st.expander("📂 " + ("File list" if lang == "en" else "文件列表")):
                        for fn in sorted(files):
                            ext = Path(fn).suffix.lower()
                            icon = "🖼" if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif"} else "📄"
                            st.write(f"{icon} {fn}")
        else:
            st.info(f"`{CASES_DIR}` " + ("has no case folders." if lang == "en" else "下未找到病例文件夹。"))
    else:
        accept_types = ["md", "txt", "pdf", "docx", "xlsx", "csv", "json", "html", "htm",
                        "png", "jpg", "jpeg", "webp", "gif"]
        uploaded_files = st.file_uploader(
            _("upload_hint", lang),
            accept_multiple_files=True,
            type=accept_types,
        )
        if uploaded_files:
            if "upload_dir" not in st.session_state:
                st.session_state.upload_dir = tempfile.mkdtemp(prefix="mdt_case_")
            upload_dir = Path(st.session_state.upload_dir)
            for uf in uploaded_files:
                safe_name = Path(uf.name).name
                (upload_dir / safe_name).write_bytes(uf.read())
            case_dir = upload_dir
            st.success(f"✅ {len(uploaded_files)} " + ("file(s) uploaded" if lang == "en" else "个文件已上传"))

    st.divider()

    col_run, col_clear = st.columns([1, 4])
    run_clicked = col_run.button(
        _("btn_run", lang), type="primary", disabled=(case_dir is None or not oc_ok)
    )
    if col_clear.button(_("btn_clear", lang)):
        if case_dir is not None:
            ws = _workspace_dir(case_dir)
            if ws.exists():
                shutil.rmtree(ws)
                st.toast("✅ " + ("Workspace cleared" if lang == "en" else "工作区已清除"))

    if not oc_ok:
        st.info("opencode CLI " + ("not installed. Run `bash scripts/setup.sh`." if lang == "en" else "未安装，请执行 `bash scripts/setup.sh`。"))

    if run_clicked and case_dir is not None:
        st.divider()
        st.subheader(_("pipeline_title", lang))
        _run_pipeline_inline(case_dir, lang)


# ══════════════════════════════════════════════════════════════════════════════
# Tab 2: Debug
# ══════════════════════════════════════════════════════════════════════════════
with tab_debug:
    st.header(_("debug_header", lang))

    discovered_debug = _discover_case_dirs()
    debug_case_dir: Optional[Path] = None

    if discovered_debug:
        debug_display = [name for name, _ in discovered_debug]
        debug_path_map = {name: path for name, path in discovered_debug}
        debug_selected = st.selectbox(
            _("select_case", lang), debug_display, key="debug_select"
        )
        if debug_selected:
            debug_case_dir = debug_path_map[debug_selected]
    else:
        st.info("No case folders found." if lang == "en" else "未找到任何病例文件夹。")

    if debug_case_dir is not None:
        ws_dir = _workspace_dir(debug_case_dir)
        if ws_dir.exists():
            st.success(f"✅ `{ws_dir}`")

            with st.expander("📄 00_manifest.json"):
                data = _read_workspace_json(ws_dir, _WS_MANIFEST)
                if data:
                    st.json(data)
                else:
                    st.info("Not generated yet." if lang == "en" else "尚未生成")

            with st.expander("📄 01_index.json"):
                data = _read_workspace_json(ws_dir, _WS_INDEX)
                if data:
                    st.json(data)
                else:
                    st.info("Not generated yet." if lang == "en" else "尚未生成")

            with st.expander("📄 02_dispatch.json"):
                data = _read_workspace_json(ws_dir, _WS_DISPATCH)
                if data:
                    _render_agents_diagram(data, {}, lang)
                    st.json(data)
                else:
                    st.info("Not generated yet." if lang == "en" else "尚未生成")

            with st.expander("💬 03_opinions — " + ("Specialist Opinions" if lang == "en" else "各专科会诊意见")):
                opinions_dir = ws_dir / _WS_OPINIONS
                if opinions_dir.is_dir():
                    op_files = sorted(opinions_dir.glob("*.md"))
                    if op_files:
                        op_dict = {f.stem: f.read_text(encoding="utf-8") for f in op_files}
                        dispatch_data = _read_workspace_json(ws_dir, _WS_DISPATCH) or {}
                        _render_agents_diagram(dispatch_data, op_dict, lang)
                        for op_file in op_files:
                            with st.expander(f"📋 {op_file.stem}"):
                                st.markdown(op_file.read_text(encoding="utf-8"))
                    else:
                        st.info("Not generated yet." if lang == "en" else "尚未生成")
                else:
                    st.info("Not generated yet." if lang == "en" else "尚未生成")

            with st.expander("🗣 04_debate.json"):
                data = _read_workspace_json(ws_dir, _WS_DEBATE)
                if data:
                    st.json(data)
                else:
                    st.info("Not generated (enable_debate=false)." if lang == "en" else "尚未生成（enable_debate=false）")

            with st.expander("📋 05_mdt_report.md"):
                report = _read_workspace_text(ws_dir, _WS_REPORT)
                if report:
                    _display_report_with_media(report, debug_case_dir, lang)
                    c1, c2, c3 = st.columns(3)
                    c1.download_button(_("dl_md", lang), report.encode(), "mdt_report.md", "text/markdown", key="dbg_dl_md")
                    c2.download_button(_("dl_html", lang), _report_to_html(report).encode(), "mdt_report.html", "text/html", key="dbg_dl_html")
                    pdf = _report_to_pdf(report)
                    if pdf:
                        c3.download_button(_("dl_pdf", lang), pdf, "mdt_report.pdf", "application/pdf", key="dbg_dl_pdf")
                else:
                    st.info("Not generated yet." if lang == "en" else "尚未生成")

            with st.expander("⚠️ " + ("Error Logs" if lang == "en" else "错误日志")):
                errors_dir = ws_dir / _WS_ERRORS
                if errors_dir.is_dir():
                    log_files = sorted(errors_dir.glob("*.log"))
                    if log_files:
                        for log_file in log_files:
                            st.markdown(f"**{log_file.name}**")
                            st.code(log_file.read_text(encoding="utf-8") or "(empty)", language=None)
                    else:
                        st.success("No error logs ✓")
                else:
                    st.info("Not generated yet." if lang == "en" else "尚未生成")

            image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
            images = [f for f in sorted(debug_case_dir.iterdir())
                      if f.is_file() and f.suffix.lower() in image_exts]
            if images:
                with st.expander("🖼 " + _("media_gallery", lang)):
                    cols = st.columns(min(len(images), 3))
                    for i, img in enumerate(images):
                        with cols[i % 3]:
                            st.image(str(img), caption=img.name, use_container_width=True)
        else:
            st.info(
                "Workspace (.mdt_workspace/) does not exist yet. Run MDT first." if lang == "en"
                else "工作区（.mdt_workspace/）尚不存在。请先运行 MDT 会诊。"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Tab 3: Admin
# ══════════════════════════════════════════════════════════════════════════════
with tab_admin:
    st.header(_("admin_header", lang))

    cfg = _load_config()

    st.subheader("OpenCode " + ("Settings" if lang == "en" else "设置"))
    oc_cfg = cfg.get("opencode", {})
    col1, col2, col3, col4 = st.columns(4)
    model_hint = "Leave blank to use opencode's built-in default" if lang == "en" else "留空使用 opencode 内置默认"
    new_model = col1.text_input(
        ("Default model" if lang == "en" else "默认模型") + f" ({model_hint})",
        value=oc_cfg.get("default_model", ""),
    )
    new_timeout = col2.number_input(
        ("Timeout (s)" if lang == "en" else "超时（秒）"),
        min_value=30, max_value=3600, value=int(oc_cfg.get("timeout", 300)),
    )
    new_max_workers = col3.number_input(
        ("Max parallel specialists" if lang == "en" else "最大并发专科数"),
        min_value=1, max_value=20, value=int(oc_cfg.get("max_workers", 5)),
    )
    new_max_rounds = col4.number_input(
        ("Max reasoning rounds (K)" if lang == "en" else "最大推理轮次（K）"),
        min_value=1, max_value=20, value=int(oc_cfg.get("max_rounds", 5)),
        help=("Each specialist agent may reason / re-read up to K rounds before writing its opinion."
              if lang == "en" else
              "每个专科 Agent 在输出意见前，最多可进行 K 轮推理（读取文件、审查证据、修正结论）。"),
    )

    st.subheader("Specialists" if lang == "en" else "专科注册表")
    specialists = cfg.get("specialists", [])
    hdr = st.columns([3, 3, 1])
    hdr[0].markdown("**" + ("Name" if lang == "en" else "专科名称") + "**")
    hdr[1].markdown("**" + ("Model override" if lang == "en" else "使用模型") + "**")
    hdr[2].markdown("**" + ("Action" if lang == "en" else "操作") + "**")

    updated_specialists = []
    for i, spec in enumerate(specialists):
        cols = st.columns([3, 3, 1])
        name_val = cols[0].text_input(f"name_{i}", value=spec.get("name", ""), label_visibility="collapsed")
        model_val = cols[1].text_input(f"model_{i}", value=spec.get("model", ""), label_visibility="collapsed")
        if not cols[2].button("✕", key=f"rm_{i}"):
            entry = {**spec, "name": name_val}
            if model_val.strip():
                entry["model"] = model_val.strip()
            else:
                entry.pop("model", None)
            updated_specialists.append(entry)

    if st.button("➕ " + ("Add specialist" if lang == "en" else "添加专科")):
        updated_specialists.append({"name": "新专科" if lang == "zh" else "new_specialist", "file_categories": []})
        st.rerun()

    st.subheader("Prompt Files" if lang == "en" else "已有 Prompt 文件")
    spec_prompt_dir = PROJECT_ROOT / "prompts" / "specialists"
    if spec_prompt_dir.is_dir():
        registered_names = {s.get("name", "") for s in updated_specialists}
        for p in sorted(spec_prompt_dir.glob("*.md")):
            name = p.stem
            en_exists = (PROJECT_ROOT / "prompts" / "en" / "specialists" / p.name).exists()
            en_badge = " 🌐" if en_exists else ""
            if name == "base":
                st.write(f"📄 `{name}.md` *(base / 通用底座)*{en_badge}")
            elif name in registered_names:
                st.write(f"✅ `{name}.md` *({'registered' if lang == 'en' else '已注册'})*{en_badge}")
            else:
                st.write(f"⚠️ `{name}.md` *({'prompt exists but not registered' if lang == 'en' else '有 Prompt 但未在注册表中'})*{en_badge}")
    else:
        st.info("`prompts/specialists/` " + ("directory not found" if lang == "en" else "目录不存在"))

    st.subheader("Workflow" if lang == "en" else "工作流设置")
    wf_cfg = cfg.get("workflow", {})
    enable_debate = st.checkbox(
        ("Enable Cross-Debate Round (Round 4)" if lang == "en" else "开启交叉讨论（Round 4 Debate）"),
        value=bool(wf_cfg.get("enable_debate", False)),
    )

    st.divider()
    if st.button(_("save_config", lang), type="primary"):
        oc_new: Dict[str, Any] = {
            "timeout": int(new_timeout),
            "max_workers": int(new_max_workers),
            "max_rounds": int(new_max_rounds),
        }
        if new_model.strip():
            oc_new["default_model"] = new_model.strip()
        new_cfg = {
            "ui": cfg.get("ui", {"language": lang}),
            "opencode": oc_new,
            "specialists": updated_specialists,
            "workflow": {**wf_cfg, "enable_debate": enable_debate},
            "paths": cfg.get("paths", {}),
        }
        _save_config(new_cfg)
        st.success("✅ " + ("Config saved to config/system.yaml" if lang == "en" else "配置已保存至 config/system.yaml"))
        st.rerun()

    with st.expander("📄 system.yaml"):
        cfg_path = PROJECT_ROOT / "config" / "system.yaml"
        if cfg_path.exists():
            st.code(cfg_path.read_text(encoding="utf-8"), language="yaml")
        else:
            st.info("Config file not found." if lang == "en" else "配置文件不存在")
