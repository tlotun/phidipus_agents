#!/usr/bin/env bash
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
# ══════════════════════════════════════════════════════════════════
#  Phidipus Agents — safe uninstaller
#  Removes the program parts (venv, launcher, caches).  Personal data
#  (AI memory, knowledge, taught workflows, config + API keys) and the
#  Brain model are only removed when you say so — and are moved to the
#  Trash (recoverable), never deleted outright.
# ══════════════════════════════════════════════════════════════════
set -uo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
G=$'\033[0;32m'; Y=$'\033[1;33m'; C=$'\033[0;36m'; N=$'\033[0m'
ok()  { echo "${G}✓${N} $*"; }
ask() { local a; read -r -p "${Y}?${N} $1 [y/N] " a </dev/tty || a=""; [[ "$a" =~ ^[yYcC] ]]; }
trash() {   # move to ~/.Trash with a unique name (recoverable)
    local p="$1" name
    [[ -e "$p" ]] || return 0
    name="$(basename "$p").phidipus-$(date +%Y%m%d-%H%M%S)"
    mv "$p" "$HOME/.Trash/$name" && ok "Đã chuyển vào Thùng rác: $p"
}

echo; echo "${C}🕷️  Gỡ cài đặt Phidipus Agents — $APP_DIR${N}"; echo

pkill -f "$APP_DIR/main.py" 2>/dev/null && ok "Đã dừng Phidipus Agents đang chạy"
PLIST="$HOME/Library/LaunchAgents/com.phidipus.agent.plist"
if [[ -f "$PLIST" ]]; then
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null
    trash "$PLIST"
fi
trash "$HOME/Applications/Phidipus Agents.app"
trash "$HOME/Applications/Phidipus.app"
trash "$APP_DIR/venv"
trash "$APP_DIR/.tools"
rm -rf "$APP_DIR/logs" /tmp/phidipus_ipc.sock 2>/dev/null
find "$APP_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null
ok "Đã gỡ phần chương trình"

echo
if ask "Xoá DỮ LIỆU CÁ NHÂN (bộ nhớ AI, kiến thức, workflow đã dạy, config.yaml + API key)?"; then
    trash "$APP_DIR/data"; trash "$APP_DIR/config.yaml"; trash "$APP_DIR/keys"
    trash "$HOME/.phidipus"; trash "$HOME/.config/phidipus"
else
    echo "  → Giữ nguyên dữ liệu (có thể cài lại và dùng tiếp)."
fi
if [[ -d "$APP_DIR/models" || -d "$APP_DIR/adapters_v7" ]] && ask "Xoá model Brain v7 (~2.5 GB)?"; then
    trash "$APP_DIR/models"; trash "$APP_DIR/adapters_v7"
fi
STATE="$HOME/.phidipus/install_state.json"
if [[ -f "$STATE" ]] && command -v curl >/dev/null && ask "Xoá các model Ollama mà trình cài đặt Phidipus Agents đã tải?"; then
    for m in $(python3 -c "import json;print(' '.join(sorted(set(json.load(open('$STATE')).get('models',{}).values()))))" 2>/dev/null); do
        curl -fsS -X DELETE http://127.0.0.1:11434/api/delete -d "{\"model\":\"$m\"}" >/dev/null 2>&1 \
            && ok "Đã xoá model $m" || echo "  (không xoá được $m — Ollama có đang chạy?)"
    done
fi
echo
echo "Ảnh/video do Phidipus Agents tạo (ví dụ ~/Desktop/AI-Images) KHÔNG bị đụng tới."
echo "Muốn xoá hẳn mã nguồn: kéo thư mục $APP_DIR vào Thùng rác."
