#!/usr/bin/env bash
# ============================================================
# setup.sh — MDT-Orchestrator 环境安装脚本
#
# 功能：
#   1. 检查 Python 3.10+ 是否可用
#   2. 安装 Python 依赖（requirements.txt）
#   3. 检查 opencode CLI 是否已安装；若未安装则尝试自动安装
#   4. （可选）安装 UI 依赖（requirements-ui.txt）
#
# 用法：
#   bash scripts/setup.sh            # 仅安装核心依赖
#   bash scripts/setup.sh --with-ui  # 同时安装 Streamlit UI 依赖
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WITH_UI=false

# ── Parse args ────────────────────────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --with-ui) WITH_UI=true ;;
        -h|--help)
            echo "Usage: $0 [--with-ui]"
            echo "  --with-ui  Also install Streamlit UI dependencies"
            exit 0
            ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────
info()    { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
success() { echo -e "\033[1;32m[OK]\033[0m    $*"; }
warn()    { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
error()   { echo -e "\033[1;31m[ERROR]\033[0m $*" >&2; }

# ── 1. Check Python ───────────────────────────────────────────────────────
info "Checking Python version …"
PYTHON_BIN=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c 'import sys; print(sys.version_info[:2])' 2>/dev/null || true)
        major=$("$candidate" -c 'import sys; print(sys.version_info[0])' 2>/dev/null || true)
        minor=$("$candidate" -c 'import sys; print(sys.version_info[1])' 2>/dev/null || true)
        if [[ "$major" -ge 3 && "$minor" -ge 10 ]]; then
            PYTHON_BIN="$candidate"
            success "Found $("$PYTHON_BIN" --version)"
            break
        fi
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    error "Python 3.10+ is required but not found on PATH."
    error "Please install Python 3.10+ and re-run this script."
    exit 1
fi

# ── 2. Install Python dependencies ────────────────────────────────────────
info "Installing Python dependencies …"
"$PYTHON_BIN" -m pip install --quiet --upgrade pip
"$PYTHON_BIN" -m pip install --quiet -r "${PROJECT_ROOT}/requirements.txt"
success "Core Python dependencies installed."

if "$WITH_UI"; then
    info "Installing UI dependencies (Streamlit) …"
    "$PYTHON_BIN" -m pip install --quiet -r "${PROJECT_ROOT}/requirements-ui.txt"
    success "UI dependencies installed."
fi

# ── 3. Check / install opencode CLI ──────────────────────────────────────
info "Checking opencode CLI …"
if command -v opencode &>/dev/null; then
    OC_VERSION=$(opencode --version 2>/dev/null || echo "unknown")
    success "opencode already installed (${OC_VERSION})."
else
    warn "opencode CLI not found on PATH. Attempting to install …"

    # Try npm first (most portable)
    if command -v npm &>/dev/null; then
        info "Installing via npm (npm install -g opencode@latest) …"
        npm install -g opencode@latest
        if command -v opencode &>/dev/null; then
            success "opencode installed via npm."
        else
            warn "npm install succeeded but 'opencode' still not on PATH."
            warn "You may need to add npm global bin to PATH:"
            warn "  export PATH=\"\$(npm prefix -g)/bin:\$PATH\""
        fi
    # Try the official curl installer
    elif command -v curl &>/dev/null; then
        info "Installing via official installer (curl) …"
        curl -fsSL https://opencode.ai/install.sh | sh
        # Reload PATH — the installer typically writes to ~/.local/bin or /usr/local/bin
        export PATH="$HOME/.local/bin:$PATH"
        if command -v opencode &>/dev/null; then
            success "opencode installed via official installer."
        else
            warn "Installer ran but 'opencode' not found on PATH. You may need to restart your shell."
        fi
    else
        error "Neither 'npm' nor 'curl' found. Cannot auto-install opencode."
        error "Please install opencode manually: https://opencode.ai/"
        exit 1
    fi
fi

# ── 4. Summary ────────────────────────────────────────────────────────────
echo ""
success "Setup complete! Quick-start commands:"
echo "  Run demo case  :  python -m src.main cases/demo_case"
if "$WITH_UI"; then
    echo "  Launch web UI  :  streamlit run app.py"
fi
echo "  Run tests      :  make test"
