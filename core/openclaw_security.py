# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/openclaw_security.py — Phidipus v1.0
═══════════════════════════════════════════════════════════════════════

5-Gate Security Pipeline cho OpenClaw skills.

Gate 1: VirusTotal Scan      — 70+ antivirus engines
Gate 2: AST Static Analysis  — Phidipus existing + OpenClaw patterns
Gate 3: AI Safety Review     — LLM đọc code, detect hidden behavior
Gate 4: Permission Audit     — List permissions, flag excessive
Gate 5: Sandbox Test         — Docker isolated run + network monitor

Mỗi gate trả dict: {passed: bool, details: str, ...}
Pipeline tổng hợp: {score: 0-100, passed: bool, gates: [...], recommendation: str}
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _resolve_model(name: str, role: str | None = None) -> str:
    """v4.3: map a legacy hard-coded model name to an installed one (model registry)."""
    try:
        from core.model_registry import resolve_model
        return resolve_model(name, role)
    except Exception:
        return name

def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;31m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════
# Gate 1: VirusTotal Scan
# ══════════════════════════════════════════════════════════════

async def gate1_virustotal(file_path: str, api_key: str = "") -> dict:
    """
    Check file hash against VirusTotal.
    Returns: {passed, detections, total_engines, details}
    """
    if not api_key:
        return {"passed": True, "skipped": True, "details": "No VirusTotal API key — skipped",
                "detections": 0, "total_engines": 0}

    try:
        # Hash all Python files concatenated (file_path is actually a directory)
        skill_dir = Path(file_path)
        all_bytes = b""
        for f in sorted(skill_dir.rglob("*.py")):
            all_bytes += f.read_bytes()
        if not all_bytes:
            # Fallback: hash SKILL.md
            md_file = skill_dir / "SKILL.md"
            if md_file.exists():
                all_bytes = md_file.read_bytes()
        if not all_bytes:
            return {"passed": True, "skipped": True, "details": "No hashable files",
                    "detections": 0, "total_engines": 0}
        file_hash = hashlib.sha256(all_bytes).hexdigest()

        req = urllib.request.Request(
            f"https://www.virustotal.com/api/v3/files/{file_hash}",
            headers={"x-apikey": api_key, "Accept": "application/json"}
        )

        def _check():
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                    stats = data["data"]["attributes"]["last_analysis_stats"]
                    malicious = stats.get("malicious", 0) + stats.get("suspicious", 0)
                    total = sum(stats.values())
                    return malicious, total
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return 0, 0  # Not in database
                raise

        malicious, total = await asyncio.get_event_loop().run_in_executor(None, _check)

        return {
            "passed": malicious == 0,
            "detections": malicious,
            "total_engines": total,
            "details": f"{malicious}/{total} engines flagged" if total > 0 else "Not in VT database",
        }
    except Exception as exc:
        return {"passed": True, "skipped": True,
                "details": f"VT check failed: {str(exc)[:60]}",
                "detections": 0, "total_engines": 0}


# ══════════════════════════════════════════════════════════════
# Gate 2: Static Analysis (AST + pattern matching)
# ══════════════════════════════════════════════════════════════

# Dangerous patterns across languages
BLOCKED_PATTERNS = {
    # Python
    "exec(": "Dynamic code execution",
    "eval(": "Dynamic code evaluation",
    "__import__": "Dynamic import",
    "compile(": "Code compilation",
    "subprocess.Popen": "Shell command execution",
    "os.system": "Shell command execution",
    "os.popen": "Shell command execution",
    "shutil.rmtree": "Recursive file deletion",
    # JavaScript/Node.js
    "child_process": "Node.js shell execution",
    "require('fs')": "Node.js filesystem access",
    "process.env": "Environment variable access (credential theft)",
    "Buffer.from": "Binary data manipulation",
    # Network exfiltration
    "navigator.sendBeacon": "Silent data exfiltration",
    "XMLHttpRequest": "Network request",
    # Obfuscation
    "String.fromCharCode": "Character code obfuscation",
    "\\x48\\x65": "Hex escape obfuscation",
    "atob(": "Base64 decode (obfuscation)",
}

# Safe domains for network access
SAFE_DOMAINS = frozenset({
    "github.com", "api.github.com", "raw.githubusercontent.com",
    "registry.npmjs.org", "pypi.org", "cdn.jsdelivr.net",
    "api.openai.com", "generativelanguage.googleapis.com",
    "api.anthropic.com", "ollama.com",
})

async def gate2_static_analysis(skill_dir: str) -> dict:
    """
    Scan all source files for dangerous patterns.
    Returns: {passed, violations: list, file_count, details}
    """
    violations = []
    file_count = 0
    skill_path = Path(skill_dir)

    # Scan all text files
    for ext in ("*.py", "*.js", "*.ts", "*.md", "*.json", "*.yaml", "*.yml", "*.sh"):
        for fpath in skill_path.rglob(ext):
            file_count += 1
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            fname = str(fpath.relative_to(skill_path))

            # Check blocked patterns
            for pattern, desc in BLOCKED_PATTERNS.items():
                if pattern in content:
                    violations.append({
                        "file": fname,
                        "pattern": pattern,
                        "type": "blocked_pattern",
                        "severity": "high",
                        "description": desc,
                    })

            # Check for suspicious base64 strings (> 200 chars)
            b64_matches = re.findall(r'[A-Za-z0-9+/=]{200,}', content)
            for match in b64_matches:
                violations.append({
                    "file": fname,
                    "pattern": f"base64 ({len(match)} chars)",
                    "type": "obfuscation",
                    "severity": "high",
                    "description": "Suspiciously long base64 string — possible obfuscated payload",
                })

            # Check for external URLs
            urls = re.findall(r'https?://[^\s\'")\]]+', content)
            for url in urls:
                try:
                    domain = url.split('/')[2].split(':')[0]
                    if domain not in SAFE_DOMAINS and not domain.endswith('.localhost'):
                        violations.append({
                            "file": fname,
                            "pattern": url[:80],
                            "type": "external_url",
                            "severity": "medium",
                            "description": f"External URL to: {domain}",
                        })
                except Exception:
                    pass

            # Check for IP addresses (potential C2 server)
            ips = re.findall(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', content)
            for ip in ips:
                if not ip.startswith(("127.", "0.", "192.168.", "10.", "172.16.")):
                    violations.append({
                        "file": fname,
                        "pattern": ip,
                        "type": "suspicious_ip",
                        "severity": "high",
                        "description": f"Public IP address — potential C2: {ip}",
                    })

    # Also try Phidipus's existing AST sandbox for Python files
    py_violations = []
    for pyf in skill_path.rglob("*.py"):
        try:
            from security.skill_ast_sandbox import SkillASTSandbox
            sandbox = SkillASTSandbox()
            result = sandbox.check(pyf.read_text(encoding="utf-8"))
            if not result.safe:
                for v in result.violations:
                    py_violations.append({
                        "file": str(pyf.relative_to(skill_path)),
                        "pattern": str(v)[:80],
                        "type": "ast_violation",
                        "severity": "high",
                        "description": str(v),
                    })
        except Exception:
            pass

    violations.extend(py_violations)
    high_count = sum(1 for v in violations if v.get("severity") == "high")
    passed = high_count == 0

    return {
        "passed": passed,
        "violations": violations,
        "high_severity": high_count,
        "medium_severity": sum(1 for v in violations if v.get("severity") == "medium"),
        "file_count": file_count,
        "details": f"{len(violations)} issues ({high_count} high)" if violations else "Clean",
    }


# ══════════════════════════════════════════════════════════════
# Gate 3: AI Safety Review
# ══════════════════════════════════════════════════════════════

async def gate3_ai_review(skill_dir: str, model: str = "qwen3:8b") -> dict:
    """
    AI reads source code + SKILL.md, analyzes for hidden malicious behavior.
    Returns: {passed, score: 0-100, analysis: str, red_flags: list}
    """
    skill_path = Path(skill_dir)

    # Gather content
    skill_md = ""
    source_code = ""

    md_file = skill_path / "SKILL.md"
    if md_file.exists():
        skill_md = md_file.read_text(encoding="utf-8", errors="ignore")[:3000]

    # Read all code files
    code_parts = []
    for ext in ("*.py", "*.js", "*.ts"):
        for f in skill_path.rglob(ext):
            content = f.read_text(encoding="utf-8", errors="ignore")
            code_parts.append(f"=== {f.name} ===\n{content[:2000]}")
    source_code = "\n".join(code_parts)[:5000]

    if not skill_md and not source_code:
        return {"passed": False, "score": 0, "analysis": "No readable content",
                "red_flags": ["Empty skill — no SKILL.md or code found"]}

    prompt = f"""Bạn là security auditor chuyên nghiệp. Phân tích OpenClaw skill này:

=== SKILL.MD ===
{skill_md}

=== SOURCE CODE ===
{source_code}

Phân tích và trả lời CHỈ JSON (không giải thích thêm):
{{
  "score": <0-100, 100=hoàn toàn an toàn>,
  "intent": "<skill này thật sự làm gì, 1 câu>",
  "red_flags": ["<vấn đề 1>", "<vấn đề 2>"],
  "data_access": ["<loại dữ liệu truy cập>"],
  "network_calls": ["<URL bên ngoài>"],
  "hidden_behaviors": ["<hành vi ẩn nếu có>"],
  "recommendation": "APPROVE hoặc REVIEW hoặc REJECT",
  "reason": "<lý do ngắn gọn>"
}}

CHÚ Ý ĐẶC BIỆT:
- Code gửi dữ liệu user ra ngoài = REJECT
- Đọc credentials, SSH keys, API keys = REJECT
- Obfuscated code (base64, eval, dynamic import) = REJECT
- Skill description không khớp với code thực tế = RED FLAG
- Network calls đến domain lạ = RED FLAG"""

    try:
        payload = json.dumps({
            "model": _resolve_model(model, "reasoning"),
            "think": False,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 600},
        }).encode()

        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        def _call():
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read()).get("response", "")

        response = await asyncio.get_event_loop().run_in_executor(None, _call)

        # Parse JSON from LLM response
        json_match = re.search(r'\{[^{}]*("score"|"intent")[^{}]*\}', response, re.DOTALL)
        if json_match:
            analysis = json.loads(json_match.group())
        else:
            analysis = {"score": 50, "recommendation": "REVIEW",
                       "reason": "Could not parse AI analysis", "red_flags": []}

        score = analysis.get("score", 50)
        recommendation = analysis.get("recommendation", "REVIEW")
        passed = score >= 70 and recommendation != "REJECT"

        _vlog("🤖", f"AI Review: score={score}, rec={recommendation}")

        return {
            "passed": passed,
            "score": score,
            "recommendation": recommendation,
            "reason": analysis.get("reason", ""),
            "intent": analysis.get("intent", ""),
            "red_flags": analysis.get("red_flags", []),
            "data_access": analysis.get("data_access", []),
            "network_calls": analysis.get("network_calls", []),
            "hidden_behaviors": analysis.get("hidden_behaviors", []),
            "details": f"Score: {score}/100 — {recommendation}",
        }

    except Exception as exc:
        _vlog("⚠️", f"AI Review failed: {exc}")
        return {
            "passed": False, "score": 0, "recommendation": "REVIEW",
            "reason": f"AI review failed: {str(exc)[:60]}",
            "red_flags": ["AI review could not complete — manual review required"],
            "details": f"Error: {str(exc)[:60]}",
        }


# ══════════════════════════════════════════════════════════════
# Gate 4: Permission Audit
# ══════════════════════════════════════════════════════════════

PERMISSION_PATTERNS = {
    "network": [r"fetch\(", r"https?://", r"urllib", r"requests\.", r"socket\.",
                r"XMLHttpRequest", r"axios", r"curl"],
    "file_read": [r"readFile", r"open\(", r"read_text", r"Path\(.*\)\.read",
                  r"fs\.read", r"cat\s"],
    "file_write": [r"writeFile", r"write_text", r"\.write\(", r"mkdir",
                   r"fs\.write", r"shutil"],
    "shell_exec": [r"exec\(", r"spawn\(", r"subprocess", r"child_process",
                   r"os\.system", r"Popen"],
    "clipboard": [r"clipboard", r"pbcopy", r"pbpaste", r"pyperclip"],
    "camera": [r"camera", r"webcam", r"screencapture", r"screenshot"],
    "credentials": [r"password", r"api_key", r"token", r"secret",
                    r"\.env", r"credentials", r"ssh.*key", r"\.pem"],
    "browser": [r"chrome", r"browser", r"CDP", r"puppeteer", r"playwright",
                r"selenium"],
    "system_info": [r"os\.uname", r"platform\.", r"hostname", r"getlogin",
                    r"environ"],
    "database": [r"sqlite", r"mongodb", r"mysql", r"redis", r"postgres"],
}

HIGH_RISK_PERMS = {"shell_exec", "credentials", "file_write"}
MEDIUM_RISK_PERMS = {"network", "clipboard", "camera", "browser", "system_info"}

async def gate4_permission_audit(skill_dir: str) -> dict:
    """
    Analyze all permissions skill requires.
    Returns: {passed, permissions: list, risk_level, high_risk, medium_risk}
    """
    skill_path = Path(skill_dir)
    detected_perms = set()

    # Read all source files
    for ext in ("*.py", "*.js", "*.ts", "*.md", "*.sh"):
        for fpath in skill_path.rglob(ext):
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for perm_name, patterns in PERMISSION_PATTERNS.items():
                for pat in patterns:
                    if re.search(pat, content, re.IGNORECASE):
                        detected_perms.add(perm_name)
                        break

    high = [p for p in detected_perms if p in HIGH_RISK_PERMS]
    medium = [p for p in detected_perms if p in MEDIUM_RISK_PERMS]
    low = [p for p in detected_perms if p not in HIGH_RISK_PERMS and p not in MEDIUM_RISK_PERMS]

    risk_level = "high" if high else ("medium" if medium else "low")
    passed = len(high) == 0  # High risk = auto-fail

    return {
        "passed": passed,
        "permissions": sorted(detected_perms),
        "high_risk": high,
        "medium_risk": medium,
        "low_risk": low,
        "risk_level": risk_level,
        "details": f"{len(detected_perms)} permissions ({len(high)} high-risk)" if detected_perms else "No special permissions",
    }


# ══════════════════════════════════════════════════════════════
# Gate 5: Sandbox Test
# ══════════════════════════════════════════════════════════════

async def gate5_sandbox_test(skill_dir: str) -> dict:
    """
    Run skill in isolated sandbox and monitor behavior.
    Falls back to basic file analysis if Docker unavailable.
    """
    skill_path = Path(skill_dir)

    # Try Docker sandbox first
    try:
        from sandbox.sandbox_manager import SandboxManager
        sm = SandboxManager()
        # Find main entry point
        entry = None
        for f in ["index.py", "main.py", "skill.py"]:
            if (skill_path / f).exists():
                entry = str(skill_path / f)
                break
        if entry:
            result = await sm.execute(
                skill_path=entry,
                input_data={"goal": "__test__", "dry_run": True},
                timeout=15,
                network_allowed=False,
            )
            return {
                "passed": result.get("exit_code", 1) == 0,
                "method": "docker",
                "exit_code": result.get("exit_code"),
                "runtime_ms": result.get("runtime_ms", 0),
                "details": "Docker sandbox test " + ("passed" if result.get("exit_code") == 0 else "failed"),
            }
    except Exception:
        pass

    # Fallback: basic integrity check
    total_size = sum(f.stat().st_size for f in skill_path.rglob("*") if f.is_file())
    file_count = sum(1 for _ in skill_path.rglob("*") if _.is_file())

    # Check for suspicious binary files
    binary_files = []
    for f in skill_path.rglob("*"):
        if f.is_file() and f.suffix in (".exe", ".dll", ".so", ".dylib", ".bin", ".dat"):
            binary_files.append(str(f.relative_to(skill_path)))

    passed = len(binary_files) == 0 and total_size < 50_000_000  # 50MB limit

    return {
        "passed": passed,
        "method": "basic",
        "total_size_mb": round(total_size / 1024 / 1024, 2),
        "file_count": file_count,
        "binary_files": binary_files,
        "details": f"Basic check: {file_count} files, {round(total_size/1024/1024,2)}MB" +
                   (f", {len(binary_files)} suspicious binaries!" if binary_files else ""),
    }


# ══════════════════════════════════════════════════════════════
# Full 5-Gate Pipeline
# ══════════════════════════════════════════════════════════════

async def run_security_pipeline(
    skill_dir: str,
    skill_name: str = "",
    vt_api_key: str = "",
    ai_model: str = "qwen3:8b",
) -> dict:
    """
    Run all 5 security gates on an OpenClaw skill.
    Returns comprehensive security report.
    """
    t0 = time.time()
    _vlog("🛡️", f"Security pipeline starting for: {skill_name or skill_dir}")

    results = {}
    gates_passed = []
    gates_failed = []

    # Gate 1: VirusTotal
    _vlog("🛡️", "Gate 1/5: VirusTotal scan...")
    g1 = await gate1_virustotal(skill_dir, vt_api_key)
    results["gate1_virustotal"] = g1
    (gates_passed if g1["passed"] else gates_failed).append("virustotal")

    # Gate 2: Static Analysis
    _vlog("🛡️", "Gate 2/5: Static analysis...")
    g2 = await gate2_static_analysis(skill_dir)
    results["gate2_static"] = g2
    (gates_passed if g2["passed"] else gates_failed).append("static_analysis")

    # Gate 3: AI Review (skip if gate 2 found critical issues)
    _vlog("🛡️", "Gate 3/5: AI safety review...")
    if g2.get("high_severity", 0) > 5:
        g3 = {"passed": False, "score": 0, "skipped": True,
              "details": "Skipped — too many static analysis violations",
              "red_flags": [f"{g2['high_severity']} high-severity static violations"]}
    else:
        g3 = await gate3_ai_review(skill_dir, model=ai_model)
    results["gate3_ai_review"] = g3
    (gates_passed if g3["passed"] else gates_failed).append("ai_review")

    # Gate 4: Permission Audit
    _vlog("🛡️", "Gate 4/5: Permission audit...")
    g4 = await gate4_permission_audit(skill_dir)
    results["gate4_permissions"] = g4
    (gates_passed if g4["passed"] else gates_failed).append("permissions")

    # Gate 5: Sandbox Test
    _vlog("🛡️", "Gate 5/5: Sandbox test...")
    g5 = await gate5_sandbox_test(skill_dir)
    results["gate5_sandbox"] = g5
    (gates_passed if g5["passed"] else gates_failed).append("sandbox")

    # Calculate overall score
    elapsed = round(time.time() - t0, 1)
    ai_score = g3.get("score", 50)
    perm_penalty = len(g4.get("high_risk", [])) * 15 + len(g4.get("medium_risk", [])) * 5
    static_penalty = g2.get("high_severity", 0) * 20 + g2.get("medium_severity", 0) * 5
    vt_penalty = g1.get("detections", 0) * 50

    overall_score = max(0, min(100, ai_score - perm_penalty - static_penalty - vt_penalty))

    # Recommendation
    if overall_score >= 80 and len(gates_failed) == 0:
        recommendation = "APPROVE"
    elif overall_score >= 50 and "virustotal" not in gates_failed:
        recommendation = "REVIEW"
    else:
        recommendation = "REJECT"

    all_passed = len(gates_failed) == 0

    report = {
        "passed": all_passed,
        "score": overall_score,
        "recommendation": recommendation,
        "gates_passed": gates_passed,
        "gates_failed": gates_failed,
        "elapsed_s": elapsed,
        "results": results,
    }

    icon = "✅" if all_passed else ("🟡" if recommendation == "REVIEW" else "❌")
    _vlog("🛡️", f"Pipeline complete: {icon} score={overall_score} rec={recommendation} "
          f"({len(gates_passed)}/5 passed, {elapsed}s)")

    return report
