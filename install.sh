#!/usr/bin/env bash
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
# ══════════════════════════════════════════════════════════════════════
#  🕷️ Phidipus Agents — one-line installer for macOS
#
#  Máy mới — mở Terminal, dán 1 dòng:
#    curl -fsSL https://raw.githubusercontent.com/tlotun/phidipus_agents/main/install.sh | bash
#  Đã có thư mục Phidipus Agents — bấm đúp "Cai_Dat_Phidipus.command".
#
#  No sudo, no Homebrew, no Xcode tools:
#    1. checks macOS / CPU
#    2. downloads the Phidipus Agents source (only when run through curl)
#    3. uv (checksum-verified) → Python 3.12 → virtualenv → packages
#    4. installer/setup_phidipus.py: keys, config, Ollama, models, Brain,
#       permissions, launcher, health check
#  Run it again any time: finished steps are skipped, downloads resume.
#  Options are passed to setup_phidipus.py (--yes, --dry-run, --profile …).
# ══════════════════════════════════════════════════════════════════════
set -euo pipefail

PHIDIPUS_REPO="${PHIDIPUS_REPO:-tlotun/phidipus_agents}"     # set by tools/prepare_release.py
PHIDIPUS_REF="${PHIDIPUS_REF:-main}"
PHIDIPUS_HOME="${PHIDIPUS_HOME:-$HOME/Applications/Phidipus}"
PY_VERSION="3.12"

if [[ -t 1 ]]; then G=$'\033[0;32m'; Y=$'\033[1;33m'; R=$'\033[0;31m'; C=$'\033[0;36m'; B=$'\033[1m'; N=$'\033[0m'
else G=""; Y=""; R=""; C=""; B=""; N=""; fi
say()  { printf '%s\n' "${C}•${N} $*"; }
ok()   { printf '%s\n' "${G}✓${N} $*"; }
warn() { printf '%s\n' "${Y}!${N} $*"; }
die()  { printf '%s\n' "${R}✗ $*${N}" >&2; exit 1; }

main() {
    printf '\n%s\n\n' "${B}${G}🕷️  Phidipus Agents — cài đặt cho macOS${N}"

    # ── 0. platform ────────────────────────────────────────────────
    [[ "$(uname -s)" == "Darwin" ]] || die "Phidipus Agents chỉ hỗ trợ macOS."
    local macos_major arch
    macos_major="$(sw_vers -productVersion | cut -d. -f1)"
    (( macos_major >= 13 )) || die "Cần macOS 13 trở lên (máy đang chạy $(sw_vers -productVersion))."
    arch="$(uname -m)"
    if [[ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" == "1" ]]; then
        arch="arm64"    # Terminal running under Rosetta on an Apple Silicon Mac
    fi
    ok "macOS $(sw_vers -productVersion) · $arch"

    # ── 1. source ──────────────────────────────────────────────────
    local script_dir="" app_dir
    if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
        script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    fi
    if [[ -n "$script_dir" && -f "$script_dir/main.py" && -f "$script_dir/installer/setup_phidipus.py" ]]; then
        app_dir="$script_dir"
        ok "Cài tại chỗ: $app_dir"
    else
        local unset_marker="__""OWNER__"   # split so release tooling never rewrites this check
        [[ "$PHIDIPUS_REPO" != "$unset_marker"* ]] || die "install.sh chưa được cấu hình repo (đặt PHIDIPUS_REPO=owner/repo)."
        app_dir="$PHIDIPUS_HOME"
        local tmp
        tmp="$(mktemp -d)"
        trap 'rm -rf "$tmp"' EXIT
        say "Tải mã nguồn $PHIDIPUS_REPO@$PHIDIPUS_REF…"
        curl -fsSL --retry 3 "https://codeload.github.com/$PHIDIPUS_REPO/tar.gz/$PHIDIPUS_REF" -o "$tmp/src.tgz" \
            || die "Không tải được mã nguồn — kiểm tra kết nối mạng."
        mkdir -p "$tmp/src"
        tar -xzf "$tmp/src.tgz" -C "$tmp/src" --strip-components 1
        [[ -f "$tmp/src/main.py" ]] || die "Gói tải về không hợp lệ."
        mkdir -p "$app_dir"
        # update in place, never touching your config, keys, data or downloaded models
        rsync -a --delete \
            --exclude 'config.yaml' --exclude 'keys/' --exclude 'data/' --exclude 'models/' \
            --exclude 'adapters_v7/' --exclude 'venv/' --exclude 'logs/' --exclude '.tools/' \
            "$tmp/src/" "$app_dir/"
        ok "Mã nguồn: $app_dir"
    fi
    cd "$app_dir"
    chmod +x ./*.command 2>/dev/null || true

    # ── 2. uv (Python package manager by Astral, checksum-verified) ─
    local uv=""
    if command -v uv >/dev/null 2>&1; then
        uv="$(command -v uv)"
    elif [[ -x ".tools/uv" ]]; then
        uv="$app_dir/.tools/uv"
    else
        local target url tmpd expected actual
        case "$arch" in arm64) target="aarch64-apple-darwin" ;; *) target="x86_64-apple-darwin" ;; esac
        url="https://github.com/astral-sh/uv/releases/latest/download/uv-$target.tar.gz"
        tmpd="$(mktemp -d)"
        say "Tải uv ($target)…"
        curl -fsSL --retry 3 "$url" -o "$tmpd/uv.tgz" || die "Không tải được uv."
        curl -fsSL --retry 3 "$url.sha256" -o "$tmpd/uv.sha256" || die "Không tải được checksum của uv."
        expected="$(awk '{print $1}' "$tmpd/uv.sha256")"
        actual="$(shasum -a 256 "$tmpd/uv.tgz" | awk '{print $1}')"
        [[ -n "$expected" && "$expected" == "$actual" ]] || die "Checksum uv không khớp — huỷ để an toàn."
        tar -xzf "$tmpd/uv.tgz" -C "$tmpd"
        mkdir -p .tools
        mv "$tmpd/uv-$target/uv" .tools/uv
        chmod +x .tools/uv
        rm -rf "$tmpd"
        uv="$app_dir/.tools/uv"
    fi
    ok "uv $("$uv" --version | awk '{print $2}')"

    # ── 3. Python 3.12 + virtualenv ────────────────────────────────
    if ! venv/bin/python -c "import sys; assert sys.version_info[:2] == (3, 12)" >/dev/null 2>&1; then
        say "Chuẩn bị Python $PY_VERSION…"
        "$uv" python install "$PY_VERSION" --quiet
        [[ -d venv ]] && mv venv "venv.broken.$(date +%s)"
        "$uv" venv --python "$PY_VERSION" --seed --quiet venv
    fi
    ok "Python $(venv/bin/python -c 'import platform; print(platform.python_version())') (venv)"

    # ── 4. packages ────────────────────────────────────────────────
    say "Cài thư viện (lần đầu ~1-3 phút)…"
    "$uv" pip install --quiet --python venv/bin/python -r requirements.txt
    ok "Thư viện đã sẵn sàng"

    # ── 5. setup steps (keys, config, Ollama, models, Brain, permissions…) ─
    if [[ -t 0 ]]; then
        exec venv/bin/python installer/setup_phidipus.py "$@"
    elif (exec </dev/tty) 2>/dev/null; then
        exec venv/bin/python installer/setup_phidipus.py "$@" </dev/tty   # curl | bash: ask on the keyboard
    else
        exec venv/bin/python installer/setup_phidipus.py --yes "$@"
    fi
}

main "$@"
