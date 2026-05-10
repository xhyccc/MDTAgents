"""
app.py — MDT-Orchestrator Web UI (Streamlit)

Tabs:
  🏥 Run MDT   — select / upload case, run pipeline, view output
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

# ──────────────────────────────────────────────────────────────────────────────
# Page config
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="MDT-Orchestrator",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers — all safe, path-controlled functions
# ──────────────────────────────────────────────────────────────────────────────

def _load_config() -> Dict[str, Any]:
    """Load system.yaml from the project root config directory."""
    cfg_path = PROJECT_ROOT / "config" / "system.yaml"
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    return {}


def _save_config(data: Dict[str, Any]) -> None:
    """Save config dict to system.yaml."""
    cfg_path = PROJECT_ROOT / "config" / "system.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, allow_unicode=True, default_flow_style=False)


def _discover_case_dirs() -> List[Tuple[str, Path]]:
    """
    Return (display_name, absolute_path) pairs for every subdirectory of
    cases/.  Only enumerated paths from our own filesystem are returned —
    no user-controlled input involved.
    """
    if not CASES_DIR.is_dir():
        return []
    return [
        (sub.name, sub.resolve())
        for sub in sorted(CASES_DIR.iterdir())
        if sub.is_dir()
    ]


def _workspace_dir(case_dir: Path) -> Path:
    """Return .mdt_workspace directory for a given case directory."""
    return case_dir / WORKSPACE_DIR_NAME


def _read_workspace_json(ws_dir: Path, filename: str) -> Optional[Any]:
    """
    Read and parse a JSON file from inside a known workspace directory.

    Both ``ws_dir`` (derived from enumerated or session-controlled paths) and
    ``filename`` (a hardcoded module-level constant) are controlled — neither
    comes from unchecked user input.
    """
    path = ws_dir / filename
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_workspace_text(ws_dir: Path, filename: str) -> Optional[str]:
    """Read a text file from inside a known workspace directory."""
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
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _run_mdt_subprocess(case_dir: Path) -> Tuple[int, str, str]:
    """Run ``python -m src.main <case_dir>`` synchronously."""
    result = subprocess.run(
        [sys.executable, "-m", "src.main", str(case_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(PROJECT_ROOT),
    )
    return result.returncode, result.stdout, result.stderr


# ──────────────────────────────────────────────────────────────────────────────
# Sidebar
# ──────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🏥 MDT-Orchestrator")
    st.caption("多学科会诊 AI 平台")
    st.divider()

    cfg = _load_config()
    oc_cfg = cfg.get("opencode", {})
    st.markdown("**当前配置**")
    st.code(
        f"模型: {oc_cfg.get('default_model', 'N/A')}\n"
        f"超时: {oc_cfg.get('timeout', 'N/A')}s\n"
        f"最大并发: {oc_cfg.get('max_workers', 'N/A')}",
        language=None,
    )

    oc_ok = _opencode_installed()
    if oc_ok:
        st.success("✅ opencode 已安装")
    else:
        st.error("❌ opencode 未找到")
        st.caption("运行 `bash scripts/setup.sh` 安装")

# ──────────────────────────────────────────────────────────────────────────────
# Tabs
# ──────────────────────────────────────────────────────────────────────────────
tab_run, tab_debug, tab_admin = st.tabs(["🏥 Run MDT", "🔍 Debug", "⚙️ Admin"])


# ══════════════════════════════════════════════════════════════════════════════
# Tab 1: Run MDT
# ══════════════════════════════════════════════════════════════════════════════
with tab_run:
    st.header("运行 MDT 会诊")

    input_mode = st.radio(
        "输入方式",
        ["📁 选择病例文件夹", "📤 上传病例文件"],
        horizontal=True,
    )

    case_dir: Optional[Path] = None

    # ── Mode A: selectbox of enumerated case directories ──────────────────
    if input_mode == "📁 选择病例文件夹":
        discovered = _discover_case_dirs()
        if discovered:
            display_names = [name for name, _ in discovered]
            path_map = {name: path for name, path in discovered}
            selected_name = st.selectbox("选择病例文件夹", display_names)
            if selected_name:
                # Path comes from filesystem enumeration, not user text input
                case_dir = path_map[selected_name]
                files = [
                    f.name for f in case_dir.iterdir()
                    if f.is_file() and not f.name.startswith(".")
                ]
                st.success(f"✅ 已选择：`{selected_name}`，包含 {len(files)} 个文件")
                if files:
                    with st.expander("查看文件列表"):
                        for fn in sorted(files):
                            st.write(f"• {fn}")
        else:
            st.info(f"在 `{CASES_DIR}` 下未找到任何病例文件夹。请先创建病例子目录。")

    # ── Mode B: file upload into a session-controlled temp dir ─────────────
    else:
        uploaded_files = st.file_uploader(
            "上传病例文件（支持 .md .txt .pdf .docx .xlsx .csv .json）",
            accept_multiple_files=True,
            type=["md", "txt", "pdf", "docx", "xlsx", "csv", "json"],
        )
        if uploaded_files:
            # Temp directory is created by tempfile — fully server-controlled
            if "upload_dir" not in st.session_state:
                st.session_state.upload_dir = tempfile.mkdtemp(prefix="mdt_case_")

            upload_dir = Path(st.session_state.upload_dir)
            for uf in uploaded_files:
                # Use only the base filename component — no path traversal possible
                safe_name = Path(uf.name).name
                (upload_dir / safe_name).write_bytes(uf.read())

            # upload_dir is from tempfile.mkdtemp() — not user input
            case_dir = upload_dir
            st.success(f"✅ 已上传 {len(uploaded_files)} 个文件")

    st.divider()

    # ── Run / clear buttons ────────────────────────────────────────────────
    col_run, col_clear = st.columns([1, 4])
    run_clicked = col_run.button(
        "▶ 开始会诊", type="primary", disabled=(case_dir is None or not oc_ok)
    )
    if col_clear.button("🗑 清除上次结果"):
        if case_dir is not None:
            ws = _workspace_dir(case_dir)
            if ws.exists():
                shutil.rmtree(ws)
                st.toast("工作区已清除")

    if not oc_ok:
        st.info("opencode CLI 未安装，无法运行会诊。请先执行 `bash scripts/setup.sh`。")

    if run_clicked and case_dir is not None:
        st.divider()
        st.subheader("会诊进度")

        output_placeholder = st.empty()
        status_placeholder = st.empty()

        with st.spinner("MDT 会诊进行中，请稍候…"):
            # case_dir is either from enumerated selectbox or tempfile — safe
            returncode, stdout, stderr = _run_mdt_subprocess(case_dir)

        if returncode == 0:
            status_placeholder.success("✅ MDT 会诊完成！")
        else:
            status_placeholder.error(f"❌ 会诊失败（exit code {returncode}）")

        combined = stdout
        if stderr:
            combined += f"\n\n--- stderr ---\n{stderr}"
        output_placeholder.code(combined, language=None)

        # Show final report using hardcoded constant filename
        ws = _workspace_dir(case_dir)
        report_text = _read_workspace_text(ws, _WS_REPORT)
        if report_text:
            st.divider()
            st.subheader("📋 MDT 最终报告")
            st.markdown(report_text)
            st.download_button(
                label="⬇ 下载报告 (.md)",
                data=report_text.encode("utf-8"),
                file_name="mdt_report.md",
                mime="text/markdown",
            )


# ══════════════════════════════════════════════════════════════════════════════
# Tab 2: Debug
# ══════════════════════════════════════════════════════════════════════════════
with tab_debug:
    st.header("Debug — 工作区文件浏览")

    discovered_debug = _discover_case_dirs()
    debug_case_dir: Optional[Path] = None

    if discovered_debug:
        debug_display = [name for name, _ in discovered_debug]
        debug_path_map = {name: path for name, path in discovered_debug}
        debug_selected = st.selectbox("选择病例文件夹（Debug）", debug_display, key="debug_select")
        if debug_selected:
            # Path from enumeration — not user text input
            debug_case_dir = debug_path_map[debug_selected]
    else:
        st.info("未找到任何病例文件夹。")

    if debug_case_dir is not None:
        ws_dir = _workspace_dir(debug_case_dir)
        if ws_dir.exists():
            st.success(f"✅ 工作区：`{ws_dir}`")

            # All workspace reads use hardcoded constant filenames
            with st.expander("📄 00_manifest.json — 文件清单"):
                data = _read_workspace_json(ws_dir, _WS_MANIFEST)
                st.json(data) if data else st.info("尚未生成")

            with st.expander("📄 01_index.json — 文件分类索引"):
                data = _read_workspace_json(ws_dir, _WS_INDEX)
                st.json(data) if data else st.info("尚未生成")

            with st.expander("📄 02_dispatch.json — 专科调度计划"):
                data = _read_workspace_json(ws_dir, _WS_DISPATCH)
                st.json(data) if data else st.info("尚未生成")

            with st.expander("💬 03_opinions — 各专科会诊意见"):
                opinions_dir = ws_dir / _WS_OPINIONS
                if opinions_dir.is_dir():
                    op_files = sorted(opinions_dir.glob("*.md"))
                    if op_files:
                        for op_file in op_files:
                            # op_files come from glob on a server-controlled dir
                            st.markdown(f"**{op_file.stem}**")
                            st.markdown(op_file.read_text(encoding="utf-8"))
                            st.divider()
                    else:
                        st.info("尚未生成")
                else:
                    st.info("尚未生成")

            with st.expander("🗣 04_debate.json — 交叉讨论（可选）"):
                data = _read_workspace_json(ws_dir, _WS_DEBATE)
                st.json(data) if data else st.info("尚未生成（或 enable_debate=false）")

            with st.expander("📋 05_mdt_report.md — 最终报告"):
                report = _read_workspace_text(ws_dir, _WS_REPORT)
                st.markdown(report) if report else st.info("尚未生成")

            with st.expander("⚠️ errors — 错误日志"):
                errors_dir = ws_dir / _WS_ERRORS
                if errors_dir.is_dir():
                    log_files = sorted(errors_dir.glob("*.log"))
                    if log_files:
                        for log_file in log_files:
                            st.markdown(f"**{log_file.name}**")
                            st.code(log_file.read_text(encoding="utf-8") or "(空)", language=None)
                    else:
                        st.success("没有错误日志 ✓")
                else:
                    st.info("尚未生成")
        else:
            st.info("工作区（.mdt_workspace/）尚不存在。请先运行 MDT 会诊。")


# ══════════════════════════════════════════════════════════════════════════════
# Tab 3: Admin
# ══════════════════════════════════════════════════════════════════════════════
with tab_admin:
    st.header("Admin — 系统配置管理")

    cfg = _load_config()

    st.subheader("OpenCode 设置")
    oc_cfg = cfg.get("opencode", {})
    col1, col2, col3 = st.columns(3)
    new_model = col1.text_input("默认模型", value=oc_cfg.get("default_model", "claude-sonnet-4"))
    new_timeout = col2.number_input(
        "超时（秒）", min_value=30, max_value=3600, value=int(oc_cfg.get("timeout", 300))
    )
    new_max_workers = col3.number_input(
        "最大并发专科数", min_value=1, max_value=20, value=int(oc_cfg.get("max_workers", 5))
    )

    st.subheader("专科注册表")
    specialists = cfg.get("specialists", [])
    spec_col_headers = st.columns([3, 3, 1])
    spec_col_headers[0].markdown("**专科名称**")
    spec_col_headers[1].markdown("**使用模型**")
    spec_col_headers[2].markdown("**操作**")

    updated_specialists = []
    for i, spec in enumerate(specialists):
        cols = st.columns([3, 3, 1])
        name_val = cols[0].text_input(
            f"名称_{i}", value=spec.get("name", ""), label_visibility="collapsed"
        )
        model_val = cols[1].text_input(
            f"模型_{i}", value=spec.get("model", new_model), label_visibility="collapsed"
        )
        if not cols[2].button("✕", key=f"remove_spec_{i}"):
            updated_specialists.append({**spec, "name": name_val, "model": model_val})

    if st.button("➕ 添加专科"):
        updated_specialists.append({"name": "新专科", "model": new_model, "file_categories": []})
        st.rerun()

    st.subheader("已有 Prompt 文件")
    spec_prompt_dir = PROJECT_ROOT / "prompts" / "specialists"
    if spec_prompt_dir.is_dir():
        registered_names = {s.get("name", "") for s in updated_specialists}
        for p in sorted(spec_prompt_dir.glob("*.md")):
            name = p.stem
            if name == "base":
                st.write(f"📄 `{name}.md` *(通用底座)*")
            elif name in registered_names:
                st.write(f"✅ `{name}.md` *(已注册)*")
            else:
                st.write(f"⚠️ `{name}.md` *(有 Prompt 但未在注册表中)*")
    else:
        st.info("prompts/specialists/ 目录不存在")

    st.subheader("工作流设置")
    wf_cfg = cfg.get("workflow", {})
    enable_debate = st.checkbox(
        "开启交叉讨论（Round 4 Debate）", value=bool(wf_cfg.get("enable_debate", False))
    )

    st.divider()
    if st.button("💾 保存配置", type="primary"):
        new_cfg = {
            "opencode": {
                "default_model": new_model,
                "timeout": int(new_timeout),
                "max_workers": int(new_max_workers),
            },
            "specialists": updated_specialists,
            "workflow": {**wf_cfg, "enable_debate": enable_debate},
            "paths": cfg.get("paths", {}),
        }
        _save_config(new_cfg)
        st.success("✅ 配置已保存至 config/system.yaml")
        st.rerun()

    with st.expander("📄 查看原始 system.yaml"):
        cfg_path = PROJECT_ROOT / "config" / "system.yaml"
        if cfg_path.exists():
            st.code(cfg_path.read_text(encoding="utf-8"), language="yaml")
        else:
            st.info("配置文件不存在")
