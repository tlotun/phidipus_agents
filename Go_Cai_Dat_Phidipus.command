#!/bin/bash
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
# 🕷️ Gỡ cài đặt Phidipus Agents — bấm đúp để chạy (dữ liệu chỉ bị xoá khi bạn đồng ý, và vào Thùng rác)
cd "$(dirname "$0")" || exit 1
bash ./installer/uninstall.sh
echo; read -r -p "Bấm Enter để đóng cửa sổ…" _
