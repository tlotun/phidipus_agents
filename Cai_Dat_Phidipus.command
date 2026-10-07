#!/bin/bash
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
# ══════════════════════════════════════════════════════════════════
#  🕷️ Cài đặt / cập nhật / sửa lỗi Phidipus Agents — bấm đúp để chạy
#  (chạy lại bất cứ lúc nào: bước đã xong sẽ được bỏ qua)
# ══════════════════════════════════════════════════════════════════
cd "$(dirname "$0")" || exit 1
# Files unpacked from a downloaded zip carry the quarantine flag; clear it on
# our own launcher scripts so macOS does not ask again for each of them.
xattr -d com.apple.quarantine ./*.command ./install.sh 2>/dev/null || true
bash ./install.sh "$@"
status=$?
echo
read -r -p "Bấm Enter để đóng cửa sổ…" _
exit $status
