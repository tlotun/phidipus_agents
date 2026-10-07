#!/usr/bin/env bash
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
# ══════════════════════════════════════════════════════════════════
#  🕷️ PHIDIPUS — Observe. Think. Execute.
#  Double-click để chạy TOÀN BỘ: Agent + Admin Panel + Telegram Bot
# ══════════════════════════════════════════════════════════════════
# KHÔNG dùng set -e → script KHÔNG dừng giữa chừng

cd "$(dirname "$0")"
PHIDIPUS_DIR="$(pwd)"

R='\033[0;31m' G='\033[0;32m' Y='\033[1;33m'
C='\033[0;36m' W='\033[1m'   N='\033[0m'

ok()   { echo -e "${G}[  OK]${N}  $*"; }
info() { echo -e "${C}[INFO]${N}  $*"; }
warn() { echo -e "${Y}[WARN]${N}  $*"; }
fail() { echo -e "${R}[FAIL]${N}  $*"; }

VENV_DIR="${PHIDIPUS_DIR}/venv"
VENV_PY="${VENV_DIR}/bin/python"

clear

# ── Show ASCII art in green ──────────────────────────────────────
ASCII_FILE="${PHIDIPUS_DIR}/phidipus-ascii.txt"
if [[ -f "$ASCII_FILE" ]]; then
    echo -e "${G}"
    cat "$ASCII_FILE"
    echo -e "${N}"
else
    echo ""
    echo -e "${G}╔══════════════════════════════════════════╗${N}"
    echo -e "${G}║      🕷️  P H I D I P U S   A G E N T S    ║${N}"
    echo -e "${G}║     Local AI • Vision Agent • macOS     ║${N}"
    echo -e "${G}╚══════════════════════════════════════════╝${N}"
    echo ""
fi

echo -e "  ${Y}▸${N} Mode: ${G}${W}AGENT THẬT${N} — LLM + Vision + IPC"
echo -e "  ${Y}▸${N} Admin Panel: ${C}http://127.0.0.1:8912${N}"
echo ""

# ══════════════════════════════════════════════════════════════════
# Kiểm tra venv
# ══════════════════════════════════════════════════════════════════
if [[ ! -f "$VENV_PY" ]]; then
    fail "Chưa có virtualenv tại: ${VENV_DIR}"
    echo -e "  ${Y}Chạy ${W}Cai_Dat_Phidipus.command${N} trước."
    read -r -p "  Bấm Enter để đóng..."; exit 1
fi
ok "Python: $($VENV_PY --version)"

# ══════════════════════════════════════════════════════════════════
# Kiểm tra Ollama
# ══════════════════════════════════════════════════════════════════
if ! curl -sf http://127.0.0.1:11434/api/tags &>/dev/null; then
    warn "Ollama chưa chạy — đang khởi động..."
    open -a Ollama 2>/dev/null || ollama serve &>/dev/null &
    sleep 3
    if curl -sf http://127.0.0.1:11434/api/tags &>/dev/null; then
        ok "Ollama — đã khởi động"
    else
        fail "Ollama không phản hồi! Mở ứng dụng Ollama thủ công."
        echo ""
        echo -e "  ${Y}Chọn chế độ:${N}"
        echo "    1) Chờ Ollama rồi thử lại"
        echo "    2) Chạy Demo mode (không cần Ollama)"
        echo "    3) Thoát"
        read -r -p "  Chọn [1/2/3]: " choice
        case "$choice" in
            2)
                info "Chạy Demo mode..."
                exec "$VENV_PY" "${PHIDIPUS_DIR}/main.py" --demo
                ;;
            3)
                exit 0
                ;;
            *)
                fail "Mở Ollama rồi chạy lại."; read -r; exit 1
                ;;
        esac
    fi
else
    MODELS=$($VENV_PY -c "
import urllib.request, json
try:
    r = urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=3)
    d = json.loads(r.read().decode())
    names = [m['name'] for m in d.get('models',[])]
    print(f'{len(names)} models: {', '.join(names[:4])}')
except: print('?')
" 2>/dev/null)
    ok "Ollama — $MODELS"
fi

# ══════════════════════════════════════════════════════════════════
# Kiểm tra config.yaml — TỰ TẠO nếu chưa có
# ══════════════════════════════════════════════════════════════════
if [[ ! -f "${PHIDIPUS_DIR}/config.yaml" || ! -f "${PHIDIPUS_DIR}/keys/memory_hmac.key" ]]; then
    # v4.3: one source of truth — the installer writes config.yaml from
    # config.example.yaml with models that fit this Mac, plus the keys.
    warn "Thiếu config.yaml / khoá bảo mật — đang tạo bằng trình cài đặt..."
    if "$VENV_PY" "${PHIDIPUS_DIR}/installer/setup_phidipus.py" --only config --yes; then
        ok "Cấu hình + khoá bảo mật — đã tạo"
        echo -e "  ${Y}★ Sau khi chạy, mở Admin Panel → Providers để thêm API key (tuỳ chọn)${N}"
    else
        fail "Không tạo được cấu hình — chạy Cai_Dat_Phidipus.command"
        read -r -p "  Bấm Enter để đóng..."; exit 1
    fi
else
    ok "config.yaml + khoá bảo mật — OK"
fi

# ══════════════════════════════════════════════════════════════════
# Kiểm tra main.py
# ══════════════════════════════════════════════════════════════════
if [[ ! -f "${PHIDIPUS_DIR}/main.py" ]]; then
    fail "Thiếu main.py! File này cần thiết để chạy agent thật."
    read -r -p "  Bấm Enter để đóng..."; exit 1
fi
ok "main.py — OK"

# ══════════════════════════════════════════════════════════════════
# Kiểm tra keys — tự tạo nếu chưa có
# ══════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════
# Build Admin Panel frontend nếu chưa có
# ══════════════════════════════════════════════════════════════════
ADMIN_UI="${PHIDIPUS_DIR}/admin/ui"

# Đồng bộ dist/ → static/ nếu dist/ là UI mới hơn
ADMIN_DIST="${PHIDIPUS_DIR}/admin/dist"
if [[ -f "${ADMIN_DIST}/index.html" ]] && grep -q "cdnjs" "${ADMIN_DIST}/index.html" 2>/dev/null; then
    mkdir -p "${PHIDIPUS_DIR}/admin/static"
    cp "${ADMIN_DIST}/index.html" "${PHIDIPUS_DIR}/admin/static/index.html" 2>/dev/null
    ok "Admin Panel UI v9.27 — đồng bộ từ dist/ ✅"
elif [[ -f "${PHIDIPUS_DIR}/admin/static/index.html" ]] && grep -q "cdnjs" "${PHIDIPUS_DIR}/admin/static/index.html" 2>/dev/null; then
    ok "Admin Panel UI v9.27 — đã có ✅"
elif [[ -f "${PHIDIPUS_DIR}/admin/static/index.html" ]]; then
    ok "Admin Panel — đã có (kiểm tra nếu cần UI mới)"
else
    warn "Admin Panel chưa có — chạy Cai_Dat_Phidipus.command trước"
fi

# ══════════════════════════════════════════════════════════════════
# Chạy Phidipus Agent Thật
# ══════════════════════════════════════════════════════════════════
echo ""
# Clean Python cache (prevents stale code)
find "${PHIDIPUS_DIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
find "${PHIDIPUS_DIR}" -name "*.pyc" -delete 2>/dev/null

# ── [FIX v4.2] Chrome CDP auto-start ─────────────────────────────
# Workflow check_email, dang_bai_linkedin cần Chrome CDP để execute JS.
# Nếu Chrome không có CDP flag → browser_execute_js fail liên tục.
if curl -sf http://127.0.0.1:9222/json/version &>/dev/null; then
    ok "Chrome CDP — đã sẵn sàng (port 9222)"
else
    info "Khởi động Chrome với CDP (remote debugging)..."
    # Đóng Chrome hiện tại (nếu đang mở không có CDP)
    if pgrep -x "Google Chrome" &>/dev/null; then
        warn "Chrome đang chạy KHÔNG có CDP — restart với CDP flag..."
        osascript -e 'tell application "Google Chrome" to quit' 2>/dev/null
        # Chờ Chrome tắt hoàn toàn (tối đa 8 giây)
        for _quit_wait in $(seq 1 8); do
            pgrep -x "Google Chrome" &>/dev/null || break
            sleep 1
        done
        sleep 1
    fi
    # Mở Chrome với CDP flag
    open -a "Google Chrome" --args --remote-debugging-port=9222 2>/dev/null
    # Chờ CDP ready (tối đa 15 giây — Chrome cần thời gian load)
    info "Chờ Chrome CDP sẵn sàng..."
    for _cdp_wait in $(seq 1 15); do
        curl -sf http://127.0.0.1:9222/json/version &>/dev/null && break
        sleep 1
    done
    if curl -sf http://127.0.0.1:9222/json/version &>/dev/null; then
        ok "Chrome CDP — đã khởi động ✅"
    else
        warn "Chrome CDP không phản hồi — workflow vẫn chạy với AppleScript fallback"
    fi
fi

info "Khởi động Phidipus Agent..."
echo ""

exec "$VENV_PY" "${PHIDIPUS_DIR}/main.py" --config "${PHIDIPUS_DIR}/config.yaml"
