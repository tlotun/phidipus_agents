#!/usr/bin/env python3
"""
tools/prepare_release.py — get Phidipus ready for a public GitHub release
═══════════════════════════════════════════════════════════════════════════

    # fill the GitHub owner/repo + contact into install.sh, README, LICENSE…
    ./venv/bin/python tools/prepare_release.py --owner YOUR_GITHUB_USERNAME   # repo: phidipus_agents

    # append the official PolyForm Noncommercial 1.0.0 text to LICENSE (downloads 4.5 KB from GitHub)
    ./venv/bin/python tools/prepare_release.py --fetch-license

    # Brain adapter release asset (paths cleaned, checksums written into installer/manifest.json)
    ./venv/bin/python tools/prepare_release.py --adapter-asset

    # final checks (secrets, license text, tests, placeholders)
    ./venv/bin/python tools/prepare_release.py --check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LICENSE_URL = ("https://raw.githubusercontent.com/polyformproject/polyform-licenses/1.0.0/"
               "PolyForm-Noncommercial-1.0.0.md")
PLACEHOLDER_FILES = ["install.sh", "README.md", "LICENSE", "NOTICE", "COMMERCIAL.md", "SECURITY.md",
                     "TRADEMARKS.md"]


def fill_placeholders(owner: str, repo: str, contact: str) -> None:
    for name in PLACEHOLDER_FILES:
        p = ROOT / name
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        new = text.replace("__OWNER__", owner).replace("__REPO__", repo)
        if contact:
            new = new.replace("__CONTACT__", contact)
        if new != text:
            p.write_text(new, encoding="utf-8")
            print(f"✓ {name}")


def fetch_license() -> int:
    lic = ROOT / "LICENSE"
    text = lic.read_text(encoding="utf-8")
    if "# PolyForm Noncommercial License 1.0.0" in text:
        print("✓ LICENSE đã chứa văn bản chính thức")
        return 0
    with urllib.request.urlopen(LICENSE_URL, timeout=30) as r:
        official = r.read().decode("utf-8")
    if not official.lstrip().startswith("# PolyForm Noncommercial License 1.0.0"):
        print("✗ Văn bản tải về không đúng định dạng — dừng")
        return 1
    head = text.split("LICENSE-TEXT-PLACEHOLDER", 1)[0].rstrip() + "\n\n"
    lic.write_text(head + official, encoding="utf-8")
    print(f"✓ Đã nối văn bản chính thức ({len(official)} ký tự) vào LICENSE")
    return 0


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def adapter_asset() -> int:
    src = ROOT / "adapters_v7"
    if not (src / "adapters.safetensors").exists():
        print("✗ Không có adapters_v7/adapters.safetensors")
        return 1
    cfg = json.loads((src / "adapter_config.json").read_text(encoding="utf-8"))
    cleaned = {}
    for k, v in cfg.items():
        if isinstance(v, str) and v.startswith("/"):
            v = Path(v).name if k != "model" else "models/" + Path(v).name
        cleaned[k] = v
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / "phidipus-brain-v7-adapter.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "adapters_v7"
        d.mkdir()
        (d / "adapter_config.json").write_text(json.dumps(cleaned, indent=4), encoding="utf-8")
        (d / "adapters.safetensors").write_bytes((src / "adapters.safetensors").read_bytes())
        sums = {f.name: _sha(f) for f in d.iterdir()}
        with tarfile.open(out, "w:gz") as tar:
            tar.add(d, arcname="adapters_v7")
    manifest_p = ROOT / "installer" / "manifest.json"
    manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
    manifest["brain"]["adapter_sha256"] = sums
    manifest_p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✓ {out.relative_to(ROOT)} ({out.stat().st_size / 1e6:.1f} MB), checksum đã ghi vào manifest")
    print("  → Tải file này lên GitHub Release, rồi đặt brain.adapter_url trong installer/manifest.json")
    print("  (Lưu ý: adapter là trọng số riêng của bạn — chỉ phát hành nếu bạn muốn người dùng có Brain v7.)")
    return 0


def check() -> int:
    problems = []
    text_lic = (ROOT / "LICENSE").read_text(encoding="utf-8")
    if "LICENSE-TEXT-PLACEHOLDER" in text_lic:
        problems.append("LICENSE chưa có văn bản chính thức (--fetch-license)")
    for name in PLACEHOLDER_FILES:
        p = ROOT / name
        if p.exists() and re.search(r"__(OWNER|REPO|CONTACT)__", p.read_text(encoding="utf-8")):
            problems.append(f"{name}: còn __OWNER__/__REPO__/__CONTACT__ (--owner/--repo/--contact)")
    from core import license_manager
    if not license_manager.PUBLIC_KEYS:
        problems.append("core/license_manager.py PUBLIC_KEYS trống (tools/issue_license.py keygen) — chỉ cần nếu bán giấy phép")
    manifest = json.loads((ROOT / "installer" / "manifest.json").read_text(encoding="utf-8"))
    if not manifest["brain"].get("adapter_url"):
        print("• (thông tin) Brain v7 giữ kín — bản công khai chạy không có tầng Brain")
    rc = subprocess.run([sys.executable, "tools/check_secrets.py"], cwd=ROOT).returncode
    if rc:
        problems.append("tools/check_secrets.py báo có bí mật")
    tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-p", "test_*.py"],
                           cwd=ROOT, capture_output=True, text=True)
    if tests.returncode:
        problems.append("unittest thất bại:\n" + tests.stderr[-800:])
    for p in problems:
        print("•", p)
    print("✓ Sẵn sàng phát hành" if not problems else f"\n{len(problems)} mục cần xử lý")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner")
    ap.add_argument("--repo", default="phidipus_agents")
    ap.add_argument("--contact", default="phidipuscom@gmail.com")
    ap.add_argument("--fetch-license", action="store_true")
    ap.add_argument("--adapter-asset", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    rc = 0
    if a.owner:
        fill_placeholders(a.owner, a.repo, a.contact)
    if a.fetch_license:
        rc |= fetch_license()
    if a.adapter_asset:
        rc |= adapter_asset()
    if a.check:
        rc |= check()
    if not any((a.owner, a.fetch_license, a.adapter_asset, a.check)):
        ap.print_help()
    return rc


if __name__ == "__main__":
    sys.exit(main())
