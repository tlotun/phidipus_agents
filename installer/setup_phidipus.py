#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
installer/setup_phidipus.py — Phidipus setup state machine (v4.3)
═══════════════════════════════════════════════════════════════════

Started by install.sh after the virtualenv + core packages exist.  Every step
is idempotent and recorded in ~/.phidipus/install_state.json, so running the
installer again resumes where it stopped (downloads resume too: Ollama pulls
and Hugging Face downloads are resumable).

Steps
  preflight    macOS / CPU / RAM / disk / network → hardware profile
  config       folders, keys (0600), config.yaml from config.example.yaml
  ollama       make sure the Ollama server runs (installs Ollama.app if needed)
  models       pull the models of the profile (first candidate that works)
  brain        Brain v7 (MLX, Apple Silicon): base model + LoRA adapter
  permissions  Accessibility / Screen Recording for Terminal
  launcher     ~/Applications/Phidipus.app (opens Phidipus in Terminal)
  doctor       health report

Usage
  ./venv/bin/python installer/setup_phidipus.py [--yes] [--dry-run]
        [--profile lite|standard|pro|max] [--full] [--skip-models]
        [--skip-brain] [--only STEP] [--doctor] [--reset]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path.home() / ".phidipus"
STATE_FILE = STATE_DIR / "install_state.json"
LOG_FILE = ROOT / "logs" / "install.log"
MANIFEST: dict[str, Any] = json.loads((ROOT / "installer" / "manifest.json").read_text(encoding="utf-8"))
OLLAMA = os.environ.get("PHIDIPUS_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

# ── terminal output ───────────────────────────────────────────────────
_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def ok(msg: str) -> None:
    print(f"  {_c('0;32', '✓')} {msg}")
    log("OK   " + msg)


def info(msg: str) -> None:
    print(f"  {_c('0;36', '•')} {msg}")
    log("INFO " + msg)


def warn(msg: str) -> None:
    print(f"  {_c('1;33', '!')} {msg}")
    log("WARN " + msg)


def fail(msg: str) -> None:
    print(f"  {_c('0;31', '✗')} {msg}")
    log("FAIL " + msg)


def header(i: int, n: int, title: str) -> None:
    print(f"\n{_c('1;36', f'[{i}/{n}] {title}')}")
    log(f"== {title}")


def progress(done: float, total: float, label: str) -> None:
    if not _TTY or total <= 0:
        return
    width = 30
    frac = max(0.0, min(1.0, done / total))
    bar = "█" * int(frac * width) + "░" * (width - int(frac * width))
    print(f"\r    {bar} {frac * 100:5.1f}%  {label[:40]:<40}", end="", flush=True)
    if frac >= 1.0:
        print()


class InstallError(RuntimeError):
    """A step failed with an actionable Vietnamese message."""


# ── context / state ───────────────────────────────────────────────────
class Ctx:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.dry = bool(args.dry_run)
        self.state: dict[str, Any] = {"version": 1, "done": {}}
        if STATE_FILE.exists() and not args.reset:
            try:
                self.state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass

    def save(self) -> None:
        if self.dry:
            return
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)

    def ask(self, question: str, default: bool = True) -> bool:
        if self.args.yes or not sys.stdin.isatty():
            return default
        suffix = "[Y/n]" if default else "[y/N]"
        try:
            ans = input(f"  ? {question} {suffix} ").strip().lower()
        except EOFError:
            return default
        return default if not ans else ans in ("y", "yes", "c", "co", "có")

    @property
    def profile(self) -> dict[str, Any]:
        return MANIFEST["profiles"][self.state.get("profile", "standard")]


def run(cmd: list[str], timeout: float = 600, check: bool = False, **kw: Any) -> subprocess.CompletedProcess:
    log("RUN  " + " ".join(cmd)[:300])
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    if check and res.returncode != 0:
        raise InstallError(f"{cmd[0]} lỗi: {(res.stderr or res.stdout).strip()[-300:]}")
    return res


def http_json(url: str, body: dict | None = None, timeout: float = 10, method: str | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8") or "{}"
    return json.loads(raw)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, label: str) -> None:
    """Resumable HTTP download with a progress bar."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
    with urllib.request.urlopen(req, timeout=60) as r:
        if have and r.status != 206:
            have = 0                                # server ignored Range → restart
        total = have + int(r.headers.get("Content-Length") or 0)
        with part.open("ab" if have else "wb") as fh:
            done = have
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                progress(done, total or done, label)
    part.replace(dest)


# ══════════════════════════════════════════════════════════════════
# Steps
# ══════════════════════════════════════════════════════════════════
def _sysctl(name: str) -> str:
    try:
        return run(["sysctl", "-n", name], timeout=5).stdout.strip()
    except Exception:
        return ""


def step_preflight(ctx: Ctx) -> None:
    mac = platform.mac_ver()[0] or "0"
    major = int(mac.split(".")[0] or 0)
    if platform.system() != "Darwin":
        raise InstallError("Phidipus Agents chỉ chạy trên macOS.")
    if major < MANIFEST["min_macos"]:
        raise InstallError(f"Cần macOS {MANIFEST['min_macos']}+ (máy đang chạy {mac}).")
    arch = platform.machine()
    rosetta = _sysctl("sysctl.proc_translated") == "1"
    ram_gb = round(int(_sysctl("hw.memsize") or 0) / (1 << 30))
    free_gb = shutil.disk_usage(ROOT).free / (1 << 30)
    ok(f"macOS {mac} · {arch}{' (Rosetta!)' if rosetta else ''} · RAM {ram_gb} GB · trống {free_gb:.0f} GB")
    if rosetta:
        warn("Python đang chạy qua Rosetta — nên dùng bản arm64 (install.sh tự chọn)")
    if arch != "arm64":
        warn("Mac Intel: Brain v7 (MLX) không chạy được; model local sẽ chậm hơn")
    if free_gb < 15:
        warn(f"Chỉ còn {free_gb:.0f} GB trống — model AI cần 5-30 GB")
    profile = ctx.args.profile or next(name for name, p in MANIFEST["profiles"].items()
                                       if ram_gb <= p["max_ram_gb"])
    ok(f"Cấu hình model: {profile} ({MANIFEST['profiles'][profile]['label']})")
    online = True
    try:
        urllib.request.urlopen(urllib.request.Request(MANIFEST["ollama_check_url"], method="HEAD"), timeout=8)
    except Exception:
        online = False
        warn("Không kết nối được internet — các bước tải về sẽ bị bỏ qua")
    ctx.state.update(macos=mac, macos_major=major, arch=arch, ram_gb=ram_gb, profile=profile, online=online)


def _gen_keys(keys: Path, dry: bool) -> None:
    files = {"memory_hmac.key": None, "skill_signing.priv": None, "skill_signing.pub": None}
    if all((keys / f).exists() for f in files):
        ok("Khoá bảo mật đã có")
        return
    if dry:
        info("[dry-run] sẽ tạo keys/ (HMAC + ed25519)")
        return
    keys.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not (keys / "memory_hmac.key").exists():
        (keys / "memory_hmac.key").write_bytes(os.urandom(32))
    if not (keys / "skill_signing.priv").exists():
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            k = Ed25519PrivateKey.generate()
            (keys / "skill_signing.priv").write_bytes(k.private_bytes(
                serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))
            (keys / "skill_signing.pub").write_bytes(k.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        except ImportError as exc:
            raise InstallError("thiếu gói cryptography — chạy lại install.sh") from exc
    for f in keys.iterdir():
        f.chmod(0o600)
    ok("Đã tạo khoá bảo mật (keys/, quyền 600)")


def _planned_models(ctx: Ctx) -> dict[str, str]:
    picks = {role: cands[0] for role, cands in ctx.profile["required"].items()}
    picks.update(ctx.state.get("models") or {})
    return picks


def _apply_models_to_config(cfg: dict[str, Any], picks: dict[str, str]) -> None:
    llm = cfg.setdefault("llm", {})
    llm["reasoning_model"] = picks.get("reasoning", llm.get("reasoning_model"))
    llm["vlm_model"] = picks.get("vision", picks.get("reasoning", llm.get("vlm_model")))
    coder = picks.get("coder") or picks.get("reasoning") or llm.get("coder_model")
    llm["coder_model"] = coder
    cfg.setdefault("skill_forge", {})["ollama_coder_model"] = coder
    cfg.setdefault("memory", {})["embedding_model"] = picks.get("embed_legacy", "nomic-embed-text")
    models = cfg.setdefault("models", {})
    for role in ("fast", "heavy", "embed"):
        if picks.get(role):
            models[role] = picks[role]


def step_config(ctx: Ctx) -> None:
    for d in ("data/skills/forge", "data/memory", "data/checkpoints", "data/plans", "data/knowledge",
              "data/workflows/scheduled", "logs"):
        if not ctx.dry:
            (ROOT / d).mkdir(parents=True, exist_ok=True)
    ok("Thư mục dữ liệu sẵn sàng")
    _gen_keys(ROOT / "keys", ctx.dry)
    cfg_path = ROOT / "config.yaml"
    if cfg_path.exists():
        ok("config.yaml đã có — giữ nguyên cấu hình của bạn")
        return
    import yaml
    cfg = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    _apply_models_to_config(cfg, _planned_models(ctx))
    if ctx.dry:
        info(f"[dry-run] sẽ tạo config.yaml (model chính: {cfg['llm']['reasoning_model']})")
        return
    cfg_path.write_text("# Generated by the Phidipus Agents installer — see config.example.yaml for docs\n"
                        + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    cfg_path.chmod(0o600)
    ctx.state["config_generated"] = True
    ok("Đã tạo config.yaml (quyền 600)")


def _ollama_up() -> bool:
    try:
        http_json(f"{OLLAMA}/api/version", timeout=3)
        return True
    except Exception:
        return False


def _wait_ollama(seconds: int = 60) -> bool:
    for _ in range(seconds):
        if _ollama_up():
            return True
        time.sleep(1)
    return False


def _find_ollama_app() -> Path | None:
    for base in (Path("/Applications"), Path.home() / "Applications"):
        app = base / "Ollama.app"
        if app.exists():
            return app
    return None


def step_ollama(ctx: Ctx) -> None:
    if _ollama_up():
        ver = http_json(f"{OLLAMA}/api/version").get("version", "?")
        ok(f"Ollama đang chạy (v{ver})")
        return
    app = _find_ollama_app()
    if app is None and shutil.which("ollama") is None:
        if ctx.dry:
            info("[dry-run] sẽ tải Ollama.app từ ollama.com")
            return
        if not ctx.state.get("online", True):
            raise InstallError("Chưa có Ollama và máy đang offline — cài Ollama từ https://ollama.com rồi chạy lại.")
        if not ctx.ask("Chưa có Ollama. Tải Ollama.app (~250 MB) từ ollama.com?"):
            raise InstallError("Cần Ollama để chạy model local — cài từ https://ollama.com rồi chạy lại.")
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "Ollama-darwin.zip"
            download(MANIFEST["ollama_download"], zip_path, "Ollama-darwin.zip")
            run(["ditto", "-x", "-k", str(zip_path), tmp], check=True)
            src = Path(tmp) / "Ollama.app"
            if not src.exists():
                raise InstallError("File tải về không chứa Ollama.app")
            if run(["codesign", "--verify", "--deep", "--strict", str(src)]).returncode != 0:
                raise InstallError("Chữ ký số của Ollama.app không hợp lệ — huỷ cài đặt")
            assess = run(["spctl", "--assess", "--type", "execute", "-vv", str(src)])
            if "accepted" not in (assess.stderr + assess.stdout):
                raise InstallError("Gatekeeper không chấp nhận Ollama.app — huỷ cài đặt")
            target = Path("/Applications") if os.access("/Applications", os.W_OK) else Path.home() / "Applications"
            target.mkdir(exist_ok=True)
            shutil.move(str(src), str(target / "Ollama.app"))
            app = target / "Ollama.app"
        ok(f"Đã cài {app} (chữ ký Apple hợp lệ)")
    if ctx.dry:
        info("[dry-run] sẽ khởi động Ollama")
        return
    if app is not None:
        run(["open", "-a", str(app)])
    else:
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    info("Đang chờ Ollama khởi động…")
    if not _wait_ollama(90):
        raise InstallError("Ollama không phản hồi — mở ứng dụng Ollama thủ công rồi chạy lại trình cài đặt.")
    ok("Ollama đã sẵn sàng")


def _installed_models() -> set[str]:
    try:
        tags = http_json(f"{OLLAMA}/api/tags", timeout=5).get("models", [])
    except Exception:
        return set()
    out = set()
    for m in tags:
        name = m.get("name", "")
        out.add(name)
        if name.endswith(":latest"):
            out.add(name[:-7])
    return out


def _installed_variant(name: str, installed: set[str]) -> str:
    if name in installed or f"{name}:latest" in installed:
        return name
    if ":" in name:
        variants = sorted(m for m in installed if m.startswith(name + "-"))
        if variants:
            return variants[0]
    return ""


def _pull(name: str) -> None:
    req = urllib.request.Request(f"{OLLAMA}/api/pull", data=json.dumps({"model": name, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=3600) as r:
        for raw in r:
            try:
                ev = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if ev.get("error"):
                raise InstallError(str(ev["error"])[:200])
            if ev.get("total"):
                progress(ev.get("completed", 0), ev["total"], f"{name}: {ev.get('status', '')}")
            if ev.get("status") == "success":
                print() if _TTY else None
                return
    raise InstallError(f"tải {name} bị ngắt")


def step_models(ctx: Ctx) -> None:
    if ctx.args.skip_models:
        info("Bỏ qua tải model (--skip-models)")
        return
    roles: dict[str, list[str]] = dict(ctx.profile["required"])
    optional = ctx.profile.get("optional") or {}
    if optional and (ctx.args.full or ctx.ask(
            f"Tải thêm model nâng cao ({', '.join(optional)}; khoảng "
            f"{sum(MANIFEST['model_sizes_gb'].get(c[0], 10) for c in optional.values()):.0f} GB)?", default=False)):
        roles.update(optional)
    installed = _installed_models()
    picks: dict[str, str] = dict(ctx.state.get("models") or {})
    for role, cands in roles.items():
        have = next((v for v in (_installed_variant(c, installed) for c in cands) if v), "")
        if have:
            picks[role] = have
            ok(f"{role:<12} {have} (đã có)")
            continue
        if ctx.dry:
            dup = cands[0] in picks.values()
            info(f"[dry-run] {role:<12} {cands[0]}"
                 + (" (dùng chung model đã liệt kê)" if dup else
                    f" sẽ tải (~{MANIFEST['model_sizes_gb'].get(cands[0], '?')} GB)"))
            picks[role] = cands[0]
            continue
        if not ctx.state.get("online", True):
            warn(f"{role}: offline — bỏ qua")
            continue
        for cand in cands:
            need = MANIFEST["model_sizes_gb"].get(cand, 5.0) + 2.0
            free = shutil.disk_usage(ROOT).free / (1 << 30)
            if free < need:
                warn(f"{cand}: cần ~{need:.0f} GB trống, chỉ còn {free:.0f} GB — thử model nhỏ hơn")
                continue
            info(f"Tải {cand} cho vai trò {role}…")
            try:
                _pull(cand)
            except (InstallError, urllib.error.URLError, OSError) as exc:
                warn(f"{cand}: {exc} — thử lựa chọn tiếp theo")
                continue
            installed.add(cand)
            picks[role] = cand
            ok(f"{role:<12} {cand}")
            break
        else:
            warn(f"{role}: chưa tải được model nào ({', '.join(cands)}) — Phidipus Agents sẽ dùng model khác đã có")
    ctx.state["models"] = picks
    if ctx.state.get("config_generated") and not ctx.dry:
        import yaml
        cfg_path = ROOT / "config.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        _apply_models_to_config(cfg, picks)
        cfg_path.write_text("# Generated by the Phidipus Agents installer — see config.example.yaml for docs\n"
                            + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
        ok("Đã ghi model đã chọn vào config.yaml")
    emb = picks.get("embed") or picks.get("embed_legacy")
    if emb and not ctx.dry:
        try:
            vec = http_json(f"{OLLAMA}/api/embed", {"model": emb, "input": "xin chào"}, timeout=60)
            if vec.get("embeddings"):
                ok(f"Embedding {emb} hoạt động")
        except Exception as exc:
            warn(f"Embedding {emb} chưa chạy được: {exc}")


def _venv_python() -> str:
    return sys.executable


def step_brain(ctx: Ctx) -> None:
    b = MANIFEST["brain"]
    if ctx.args.skip_brain:
        info("Bỏ qua Brain v7 (--skip-brain)")
        return
    if ctx.state.get("arch") != "arm64" or ctx.state.get("macos_major", 0) < MANIFEST["brain_min_macos"]:
        info("Brain v7 cần Apple Silicon + macOS 14 — bỏ qua (agent vẫn chạy đầy đủ, chỉ thiếu tầng phân loại Brain)")
        return
    model_dir, adapter_dir = ROOT / b["model_dir"], ROOT / b["adapter_dir"]
    # The LoRA adapter is what makes the Brain useful — without it (private
    # adapter, empty adapter_url) skip BEFORE downloading ~9 GB of base model.
    if not (adapter_dir / "adapters.safetensors").exists() and not b.get("adapter_url"):
        info("Brain v7 không có trong bản công khai (adapter riêng) — bỏ qua; agent vẫn chạy đầy đủ, "
             "chỉ thiếu tầng phân loại Brain")
        return
    if run([_venv_python(), "-c", "import mlx_lm"]).returncode != 0:
        if ctx.dry:
            info("[dry-run] sẽ cài requirements/brain.txt (mlx, mlx-lm)")
        else:
            uv = shutil.which("uv") or str(ROOT / ".tools" / "uv")
            info("Cài MLX cho Brain v7…")
            run([uv, "pip", "install", "--python", _venv_python(), "-r", str(ROOT / "requirements" / "brain.txt")],
                timeout=1800, check=True)
            ok("Đã cài mlx + mlx-lm")
    if (model_dir / "config.json").exists() and (model_dir / "model.safetensors").exists():
        ok(f"Model gốc Brain có sẵn ({b['model_dir']})")
    elif ctx.dry:
        info(f"[dry-run] sẽ tải/convert model gốc vào {b['model_dir']}")
    elif not ctx.state.get("online", True):
        warn("Offline — chưa có model gốc Brain, bỏ qua")
        return
    elif b.get("base_hf_repo"):
        info(f"Tải model gốc từ Hugging Face: {b['base_hf_repo']}")
        code = ("from huggingface_hub import snapshot_download;"
                f"snapshot_download(repo_id={b['base_hf_repo']!r}, local_dir={str(model_dir)!r})")
        run([_venv_python(), "-c", code], timeout=7200, check=True)
        ok("Đã tải model gốc Brain")
    elif ctx.ask(f"Tự tạo model gốc Brain từ {b['base_convert_from']} (tải ~9 GB rồi lượng tử 4-bit)?", default=True):
        info("Đang convert (có thể mất 10-30 phút)…")
        run([_venv_python(), "-m", "mlx_lm", "convert", "--hf-path", b["base_convert_from"], "-q",
             "--q-bits", "4", "--mlx-path", str(model_dir)], timeout=7200, check=True)
        ok("Đã tạo model gốc Brain")
    else:
        warn("Chưa có model gốc Brain — bỏ qua tầng Brain")
        return
    if not ctx.dry:
        for fname, want in (b.get("base_sha256") or {}).items():
            f = model_dir / fname
            if f.exists() and sha256_file(f) != want:
                warn(f"{fname}: checksum khác bản đã huấn luyện adapter — Brain vẫn chạy nhưng có thể kém chính xác hơn")
    if (adapter_dir / "adapters.safetensors").exists():
        ok("Adapter Brain v7 có sẵn")
    elif b.get("adapter_url") and not ctx.dry:
        info("Tải adapter Brain v7…")
        with tempfile.TemporaryDirectory() as tmp:
            tgz = Path(tmp) / "adapter.tar.gz"
            download(b["adapter_url"], tgz, "adapter.tar.gz")
            adapter_dir.mkdir(parents=True, exist_ok=True)
            run(["tar", "-xzf", str(tgz), "-C", str(adapter_dir), "--strip-components", "1"], check=True)
        ok("Đã tải adapter")
    else:
        warn("Chưa có adapter Brain v7 (adapter_url trống) — Phidipus Agents chạy không có tầng Brain")
        return
    for fname, want in (b.get("adapter_sha256") or {}).items():
        f = adapter_dir / fname
        if f.exists() and sha256_file(f) != want:
            raise InstallError(f"{fname} sai checksum — file adapter có thể bị hỏng hoặc bị sửa")
    ok("Brain v7 sẵn sàng")


_PANES = {
    "Accessibility": ["x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility",
                      "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
    "Screen Recording": ["x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture",
                         "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"],
}


def _perm_status() -> dict[str, bool | None]:
    code = ("import json\n"
            "out={}\n"
            "try:\n from ApplicationServices import AXIsProcessTrusted; out['Accessibility']=bool(AXIsProcessTrusted())\n"
            "except Exception: out['Accessibility']=None\n"
            "try:\n import Quartz; out['Screen Recording']=bool(Quartz.CGPreflightScreenCaptureAccess())\n"
            "except Exception: out['Screen Recording']=None\n"
            "print(json.dumps(out))")
    try:
        return json.loads(run([_venv_python(), "-c", code], timeout=20).stdout.strip() or "{}")
    except Exception:
        return {}


def step_permissions(ctx: Ctx) -> None:
    status = _perm_status()
    missing = [k for k, v in status.items() if v is False]
    for k, v in status.items():
        (ok if v else warn)(f"{k}: {'đã cấp' if v else 'CHƯA cấp' if v is False else 'không kiểm tra được'}")
    if not missing:
        return
    term = os.environ.get("TERM_PROGRAM", "Terminal").replace("Apple_Terminal", "Terminal")
    info(f"Phidipus Agents chạy trong {term}: hãy BẬT {term} trong các mục: {', '.join(missing)}.")
    info("Automation (điều khiển Chrome / System Events) sẽ được macOS hỏi ở lần dùng đầu — chọn OK.")
    if ctx.dry:
        return
    for name in missing:
        for url in _PANES[name]:
            if run(["open", url]).returncode == 0:
                break
    if ctx.ask("Đã bật xong quyền? (có thể cần mở lại Terminal để có hiệu lực)", default=True):
        again = _perm_status()
        still = [k for k in missing if again.get(k) is False]
        if still:
            warn(f"Chưa nhận quyền {', '.join(still)} — đóng và mở lại {term}, rồi chạy: "
                 f"./venv/bin/python installer/setup_phidipus.py --doctor")


def step_launcher(ctx: Ctx) -> None:
    for f in ROOT.glob("*.command"):
        if not ctx.dry:
            f.chmod(f.stat().st_mode | 0o111)
    app = Path.home() / "Applications" / "Phidipus Agents.app"
    if ctx.dry:
        info(f"[dry-run] sẽ tạo {app}")
        return
    app.parent.mkdir(exist_ok=True)
    root_q = str(ROOT).replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'on run\n'
        '  tell application "Terminal"\n'
        '    activate\n'
        f'    do script "cd " & quoted form of "{root_q}" & " && ./Phidipus_Agent.command"\n'
        '  end tell\n'
        'end run\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        src = fh.name
    try:
        if app.exists():
            shutil.rmtree(app)
        run(["osacompile", "-o", str(app), src], check=True)
    finally:
        os.unlink(src)
    logo = ROOT / "admin" / "static" / "logo-phidipus.png"
    icns = app / "Contents" / "Resources" / "applet.icns"
    if logo.exists():
        run(["sips", "-s", "format", "icns", str(logo), "--out", str(icns)])
    ok(f"Đã tạo {app} — mở bằng Spotlight: gõ “Phidipus Agents”")


def _require(cond: Any, msg: str) -> Any:
    if not cond:
        raise RuntimeError(msg)
    return cond


def step_doctor(ctx: Ctx) -> int:
    problems = 0
    sys.path.insert(0, str(ROOT))

    def check(label: str, fn: Callable[[], Any], critical: bool = True) -> None:
        nonlocal problems
        try:
            res = fn()
            if res is False:
                raise RuntimeError("không đạt")
            ok(f"{label}{(': ' + str(res)) if res not in (True, None) else ''}")
        except Exception as exc:
            (fail if critical else warn)(f"{label}: {str(exc)[:120]}")
            problems += 1 if critical else 0

    check("Python", lambda: platform.python_version())
    for mod in ("fastapi", "yaml", "telegram", "numpy"):
        check(f"Gói {mod}", lambda m=mod: __import__(m) and True)
    if platform.system() == "Darwin":
        check("PyObjC Quartz", lambda: __import__("Quartz") and True)
        check("atomacos (Accessibility)", lambda: __import__("atomacos") and True, critical=False)

    def _cfg():
        from config.config_loader import PhidipusConfig
        return PhidipusConfig.from_file(str(ROOT / "config.yaml")).llm.reasoning_model
    check("config.yaml", _cfg)
    for f in ("memory_hmac.key", "skill_signing.priv"):
        check(f"keys/{f}", lambda f=f: (ROOT / "keys" / f).exists())
    check("Ollama", lambda: _require(_ollama_up(), "chưa chạy"))

    def _roles():
        import yaml
        from core import model_registry as mr
        mr.configure(yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")))
        reg = mr.get_registry()
        reg.refresh(force=True)
        _require(reg.is_reachable(), "chưa kiểm tra được (Ollama chưa chạy)")
        out = []
        for role in ("reasoning", "fast", "vision", "embed"):
            m = reg.get_model(role)
            out.append(f"{role}={m}{'' if reg.is_installed(m) else ' (CHƯA CÓ)'}")
        return ", ".join(out)
    check("Model", _roles, critical=False)
    b = MANIFEST["brain"]
    check("Brain v7", lambda: _require((ROOT / b["model_dir"] / "config.json").exists()
                                       and (ROOT / b["adapter_dir"] / "adapters.safetensors").exists(),
                                       "chưa có (tuỳ chọn)"), critical=False)
    for k, v in _perm_status().items():
        check(f"Quyền {k}", lambda v=v: _require(v is not False, "chưa cấp"), critical=False)

    def _port():
        import socket
        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", 8912)) != 0 or "đang được dùng (Phidipus Agents đang chạy?)"
    check("Cổng 8912", _port, critical=False)
    print()
    if problems:
        fail(f"{problems} vấn đề bắt buộc — xem ở trên, sửa rồi chạy lại trình cài đặt")
    else:
        ok("Mọi kiểm tra bắt buộc đều đạt")
    return problems


STEPS: list[tuple[str, str, Callable[[Ctx], Any], bool]] = [
    # (key, title, fn, re-run every time)
    ("preflight", "Kiểm tra máy", step_preflight, True),
    ("config", "Cấu hình + khoá bảo mật", step_config, False),
    ("ollama", "Ollama (máy chạy model AI)", step_ollama, True),
    ("models", "Model AI theo RAM", step_models, True),
    ("brain", "Brain v7 (MLX)", step_brain, False),
    ("permissions", "Quyền macOS", step_permissions, True),
    ("launcher", "Biểu tượng khởi chạy", step_launcher, False),
    ("doctor", "Kiểm tra tổng thể", step_doctor, True),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phidipus Agents installer (setup steps)")
    ap.add_argument("--yes", "-y", action="store_true", help="answer yes to every question")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    ap.add_argument("--profile", choices=list(MANIFEST["profiles"]), help="force a model profile")
    ap.add_argument("--full", action="store_true", help="also pull optional heavy/coder models")
    ap.add_argument("--skip-models", action="store_true")
    ap.add_argument("--skip-brain", action="store_true")
    ap.add_argument("--only", choices=[s[0] for s in STEPS], help="run a single step")
    ap.add_argument("--doctor", action="store_true", help="only run the health report")
    ap.add_argument("--reset", action="store_true", help="forget previous progress")
    args = ap.parse_args(argv)
    ctx = Ctx(args)
    if args.doctor:
        args.only = "doctor"
        if "profile" not in ctx.state:
            step_preflight(ctx)
    print(_c("1;32", "\n🕷️  Phidipus Agents — trình cài đặt" + ("  (DRY-RUN: không thay đổi gì)" if ctx.dry else "")))
    steps = [s for s in STEPS if not args.only or s[0] == args.only]
    if args.only and args.only != "preflight" and "profile" not in ctx.state:
        step_preflight(ctx)
    for i, (key, title, fn, always) in enumerate(steps, 1):
        header(i, len(steps), title)
        if key in ctx.state["done"] and not always and not args.only:
            ok("đã hoàn tất ở lần chạy trước")
            continue
        try:
            rc = fn(ctx)
        except InstallError as exc:
            fail(str(exc))
            ctx.save()
            print(_c("1;33", "\n  Chạy lại trình cài đặt để tiếp tục — các bước đã xong sẽ được bỏ qua."))
            print(f"  Nhật ký: {LOG_FILE}")
            return 1
        except KeyboardInterrupt:
            ctx.save()
            print(_c("1;33", "\n  Đã dừng. Chạy lại để tiếp tục từ bước này."))
            return 130
        if key == "doctor":
            ctx.save()
            return 1 if rc else 0
        ctx.state["done"][key] = time.time()
        ctx.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
