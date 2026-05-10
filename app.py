"""
app.py — MDT-Orchestrator Web UI (Streamlit)

Tabs:
  🏥 Run MDT   — specify case folder, upload files, run pipeline, view output
  🔍 Debug     — browse all workspace intermediate files (manifest, index, etc.)
  ⚙️ Admin     — view / edit system config, inspect registered specialists
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import streamlit as st
import yaml

# ── Project root (two levels up from this file if inside src/, or same dir) ──
PROJECT_ROOT = Path(__file__).resolve().parent
WORKSPACE_DIR_NAME = ".mdt_workspace"

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


def _workspace_dir(case_dir: Path) -> Path:
    return case_dir / WORKSPACE_DIR_NAME


def _read_json_file(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
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


def _run_mdt_subprocess(case_dir: Path) -> tuple[int, str, str]:
    """Run ``python -m src.main <case_dir>`` and return (returncode, stdout, stderr)."""
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
        ["📁 指定病例文件夹路径", "📤 上传病例文件"],
        horizontal=True,
    )

    case_dir: Optional[Path] = None

    # ── Mode A: folder path ────────────────────────────────────────────────
    if input_mode == "📁 指定病例文件夹路径":
        default_case = str(PROJECT_ROOT / "cases" / "demo_case")
        folder_path = st.text_input(
            "病例文件夹绝对路径",
            value=default_case,
            placeholder="/path/to/case_folder",
        )
        if folder_path:
            p = Path(folder_path)
            if p.exists() and p.is_dir():
                case_dir = p
                files = [f.name for f in p.iterdir() if f.is_file() and f.name[0] != "."]
                st.success(f"✅ 文件夹已找到，包含 {len(files)} 个文件")
                if files:
                    with st.expander("查看文件列表"):
                        for fn in sorted(files):
                            st.write(f"• {fn}")
            else:
                st.warning("⚠️ 路径不存在或不是文件夹")

    # ── Mode B: file upload ────────────────────────────────────────────────
    else:
        uploaded_files = st.file_uploader(
            "上传病例文件（支持 .md .txt .pdf .docx .xlsx .csv .json）",
            accept_multiple_files=True,
            type=["md", "txt", "pdf", "docx", "xlsx", "csv", "json"],
        )
        if uploaded_files:
            # Persist uploads to a temp directory within the session
            if "upload_dir" not in st.session_state:
                st.session_state.upload_dir = tempfile.mkdtemp(prefix="mdt_case_")

            upload_dir = Path(st.session_state.upload_dir)
            for uf in uploaded_files:
                (upload_dir / uf.name).write_bytes(uf.read())

            case_dir = upload_dir
            st.success(f"✅ 已上传 {len(uploaded_files)} 个文件")

    st.divider()

    # ── Run button ─────────────────────────────────────────────────────────
    col_run, col_clear = st.columns([1, 4])
    run_clicked = col_run.button("▶ 开始会诊", type="primary", disabled=(case_dir is None or not oc_ok))
    if col_clear.button("🗑 清除上次结果"):
        if case_dir:
            ws = _workspace_dir(case_dir)
            if ws.exists():
                import shutil
                shutil.rmtree(ws)
                st.toast("工作区已清除")

    if not oc_ok:
        st.info("opencode CLI 未安装，无法运行会诊。请先执行 `bash scripts/setup.sh`。")

    if run_clicked and case_dir:
        st.divider()
        st.subheader("会诊进度")

        output_placeholder = st.empty()
        status_placeholder = st.empty()

        with st.spinner("MDT 会诊进行中，请稍候…"):
            returncode, stdout, stderr = _run_mdt_subprocess(case_dir)

        if returncode == 0:
            status_placeholder.success("✅ MDT 会诊完成！")
        else:
            status_placeholder.error(f"❌ 会诊失败（exit code {returncode}）")

        # Show combined output
        combined = stdout
        if stderr:
            combined += f"\n\n--- stderr ---\n{stderr}"
        output_placeholder.code(combined, language=None)

        # Show final report if available
        ws = _workspace_dir(case_dir)
        report_path = ws / "05_mdt_report.md"
        if report_path.exists():
            st.divider()
            st.subheader("📋 MDT 最终报告")
            report_text = report_path.read_text(encoding="utf-8")
            st.markdown(report_text)

            # Download button
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

    debug_case_path = st.text_input(
        "病例文件夹路径（Debug）",
        value=str(PROJECT_ROOT / "cases" / "demo_case"),
        key="debug_case_path",
    )

    debug_case_dir = Path(debug_case_path) if debug_case_path else None
    ws_dir = _workspace_dir(debug_case_dir) if debug_case_dir else None

    if debug_case_dir and debug_case_dir.exists():
        if ws_dir and ws_dir.exists():
            st.success(f"✅ 工作区已找到：`{ws_dir}`")

            # ── 00_manifest.json ──────────────────────────────────────────
            with st.expander("📄 00_manifest.json — 文件清单"):
                data = _read_json_file(ws_dir / "00_manifest.json")
                if data:
                    st.json(data)
                else:
                    st.info("尚未生成")

            # ── 01_index.json ─────────────────────────────────────────────
            with st.expander("📄 01_index.json — 文件分类索引"):
                data = _read_json_file(ws_dir / "01_index.json")
                if data:
                    st.json(data)
                else:
                    st.info("尚未生成")

            # ── 02_dispatch.json ──────────────────────────────────────────
            with st.expander("📄 02_dispatch.json — 专科调度计划"):
                data = _read_json_file(ws_dir / "02_dispatch.json")
                if data:
                    st.json(data)
                else:
                    st.info("尚未生成")

            # ── 03_opinions/*.md ──────────────────────────────────────────
            opinions_dir = ws_dir / "03_opinions"
            with st.expander("💬 03_opinions — 各专科会诊意见"):
                if opinions_dir.exists():
                    opinion_files = sorted(opinions_dir.glob("*.md"))
                    if opinion_files:
                        for op_file in opinion_files:
                            st.markdown(f"**{op_file.stem}**")
                            st.markdown(op_file.read_text(encoding="utf-8"))
                            st.divider()
                    else:
                        st.info("尚未生成")
                else:
                    st.info("尚未生成")

            # ── 04_debate.json ────────────────────────────────────────────
            with st.expander("🗣 04_debate.json — 交叉讨论（可选）"):
                data = _read_json_file(ws_dir / "04_debate.json")
                if data:
                    st.json(data)
                else:
                    st.info("尚未生成（或 enable_debate=false）")

            # ── 05_mdt_report.md ──────────────────────────────────────────
            with st.expander("📋 05_mdt_report.md — 最终报告"):
                report_path = ws_dir / "05_mdt_report.md"
                if report_path.exists():
                    st.markdown(report_path.read_text(encoding="utf-8"))
                else:
                    st.info("尚未生成")

            # ── Error logs ────────────────────────────────────────────────
            errors_dir = ws_dir / "errors"
            with st.expander("⚠️ errors — 错误日志"):
                if errors_dir.exists():
                    error_logs = sorted(errors_dir.glob("*.log"))
                    if error_logs:
                        for log_file in error_logs:
                            st.markdown(f"**{log_file.name}**")
                            content = log_file.read_text(encoding="utf-8")
                            st.code(content or "(空)", language=None)
                    else:
                        st.success("没有错误日志 ✓")
                else:
                    st.info("尚未生成")
        else:
            st.info("工作区（.mdt_workspace/）尚不存在。请先运行 MDT 会诊。")
    elif debug_case_path:
        st.warning("路径不存在")


# ══════════════════════════════════════════════════════════════════════════════
# Tab 3: Admin
# ══════════════════════════════════════════════════════════════════════════════
with tab_admin:
    st.header("Admin — 系统配置管理")

    cfg = _load_config()

    # ── opencode settings ──────────────────────────────────────────────────
    st.subheader("OpenCode 设置")
    oc_cfg = cfg.get("opencode", {})

    col1, col2, col3 = st.columns(3)
    new_model = col1.text_input("默认模型", value=oc_cfg.get("default_model", "claude-sonnet-4"))
    new_timeout = col2.number_input("超时（秒）", min_value=30, max_value=3600, value=int(oc_cfg.get("timeout", 300)))
    new_max_workers = col3.number_input("最大并发专科数", min_value=1, max_value=20, value=int(oc_cfg.get("max_workers", 5)))

    # ── Specialist registry ────────────────────────────────────────────────
    st.subheader("专科注册表")
    specialists = cfg.get("specialists", [])

    spec_col_headers = st.columns([3, 3, 1])
    spec_col_headers[0].markdown("**专科名称**")
    spec_col_headers[1].markdown("**使用模型**")
    spec_col_headers[2].markdown("**操作**")

    updated_specialists = []
    for i, spec in enumerate(specialists):
        cols = st.columns([3, 3, 1])
        name_val = cols[0].text_input(f"名称_{i}", value=spec.get("name", ""), label_visibility="collapsed")
        model_val = cols[1].text_input(f"模型_{i}", value=spec.get("model", new_model), label_visibility="collapsed")
        remove = cols[2].button("✕", key=f"remove_spec_{i}")
        if not remove:
            updated_specialists.append({**spec, "name": name_val, "model": model_val})

    if st.button("➕ 添加专科"):
        updated_specialists.append({"name": "新专科", "model": new_model, "file_categories": []})
        st.rerun()

    # ── Available prompt files ─────────────────────────────────────────────
    st.subheader("已有 Prompt 文件")
    spec_prompt_dir = PROJECT_ROOT / "prompts" / "specialists"
    if spec_prompt_dir.exists():
        prompt_files = sorted(spec_prompt_dir.glob("*.md"))
        prompt_names = [p.stem for p in prompt_files]
        registered_names = [s.get("name", "") for s in updated_specialists]
        for pn in prompt_names:
            if pn == "base":
                st.write(f"📄 `{pn}.md` *(通用底座)*")
            elif pn in registered_names:
                st.write(f"✅ `{pn}.md` *(已注册)*")
            else:
                st.write(f"⚠️ `{pn}.md` *(有 Prompt 但未在注册表中)*")
    else:
        st.info("prompts/specialists/ 目录不存在")

    # ── Workflow settings ──────────────────────────────────────────────────
    st.subheader("工作流设置")
    wf_cfg = cfg.get("workflow", {})
    enable_debate = st.checkbox("开启交叉讨论（Round 4 Debate）", value=bool(wf_cfg.get("enable_debate", False)))

    # ── Save button ────────────────────────────────────────────────────────
    st.divider()
    if st.button("💾 保存配置", type="primary"):
        new_cfg = {
            "opencode": {
                "default_model": new_model,
                "timeout": int(new_timeout),
                "max_workers": int(new_max_workers),
            },
            "specialists": updated_specialists,
            "workflow": {
                **wf_cfg,
                "enable_debate": enable_debate,
            },
            "paths": cfg.get("paths", {}),
        }
        _save_config(new_cfg)
        st.success("✅ 配置已保存至 config/system.yaml")
        st.rerun()

    # ── Raw config view ────────────────────────────────────────────────────
    with st.expander("📄 查看原始 system.yaml"):
        cfg_path = PROJECT_ROOT / "config" / "system.yaml"
        if cfg_path.exists():
            st.code(cfg_path.read_text(encoding="utf-8"), language="yaml")
        else:
            st.info("配置文件不存在")
