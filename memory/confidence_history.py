# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory/confidence_history.py — Phidipus B2 Dynamic Confidence Calibration v2.2
════════════════════════════════════════════════════════════════════════════════

B2: Dynamic Confidence Calibration — Tự hiệu chỉnh confidence per element.

Vấn đề:
  ConfidenceGate hardcode threshold = 0.8 (architectural constant R-21).
  Nhưng có những element như "Facebook Post button" mà VLM luôn trả về
  conf=0.76 (do ánh sáng, theme tối/sáng) nhưng 100% click đúng.
  → Bị gate chặn oan, phải leo lên S4 VLM tốn thêm 3-25s.

  Ngược lại, có element conf=0.91 nhưng thực tế fail 30% (VLM tự tin nhầm).

Giải pháp — ConfidenceHistory:
  KHÔNG hạ CONFIDENCE_THRESHOLD (vi phạm R-21).
  Thay vào đó: bổ sung "trust bonus" dựa trên lịch sử thực tế.

  reported_confidence_adjusted = raw_conf + trust_bonus
  trust_bonus = f(historical_success_rate, sample_count)

  Ví dụ:
    fb_post_button: raw_conf=0.76, success_rate=1.0, 47 samples
    → trust_bonus = +0.06 → adjusted = 0.82 → PASS gate ✅

    unknown_button: raw_conf=0.91, success_rate=0.55, 11 samples
    → trust_penalty = -0.08 → adjusted = 0.83 → vẫn pass nhưng
    → force_verify = True → ClickVerifier bắt buộc

Schema (data/memory/confidence_history.json):
{
  "facebook.com": {
    "fb_post_button": {
      "samples": [
        {"conf": 0.76, "success": true,  "strategy": "zoom_fine", "ts": 1234567890},
        {"conf": 0.82, "success": true,  "strategy": "memory",    "ts": 1234567891}
      ],
      "success_rate": 0.97,
      "avg_conf":     0.79,
      "trust_bonus":  0.06,
      "force_verify": false,
      "sample_count": 47,
      "last_updated": "2026-03-24"
    }
  }
}

Integration points:
  1. vision_actor.find_and_click_v2() — SAU khi lấy (x,y,conf,method):
     adjusted = conf_hist.adjust(domain, action_key, raw_conf)
     if adjusted.force_verify: bật verify bắt buộc
     conf = adjusted.confidence  ← dùng adjusted thay raw để pass gate

  2. vision_actor.find_and_click_v2() — SAU khi click+verify:
     conf_hist.record(domain, action_key, conf, success, method)
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ── Constants ─────────────────────────────────────────────────────
# R-21: CONFIDENCE_THRESHOLD = 0.8 là bất biến kiến trúc — KHÔNG thay đổi.
# trust_bonus tối đa được phép thêm vào confidence.
_MAX_TRUST_BONUS: float  = 0.10   # không thể boost quá 0.10
_MIN_TRUST_PENALTY: float = -0.10  # không thể phạt quá 0.10
_MIN_SAMPLES_FOR_CALIBRATION: int = 5   # cần ít nhất 5 mẫu
_MAX_SAMPLES_PER_ELEMENT: int = 50      # giữ 50 mẫu gần nhất


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class ConfSample:
    """1 mẫu confidence thực tế."""
    conf:     float
    success:  bool
    strategy: str   # "memory"|"zoom_*"|"consensus"|"crop"|"vlm_*"
    ts:       float = field(default_factory=time.time)


@dataclass
class AdjustedConf:
    """
    Kết quả sau khi hiệu chỉnh confidence.
    Dùng self.confidence thay vì raw_conf để pass/fail ConfidenceGate.
    """
    confidence:   float          # raw_conf + trust_bonus (clamped 0.0–1.0)
    raw_conf:     float          # confidence gốc từ VLM/strategy
    trust_bonus:  float          # delta được áp dụng (âm = penalty)
    force_verify: bool = False   # True → bắt buộc chạy ClickVerifier dù conf cao
    source:       str  = ""      # lý do hiệu chỉnh (để log)
    sample_count: int  = 0


@dataclass
class ElementHistory:
    """Lịch sử confidence cho 1 (domain, action_key)."""
    domain:       str
    action_key:   str
    samples:      list[ConfSample] = field(default_factory=list)
    success_rate: float = 1.0
    avg_conf:     float = 0.8
    trust_bonus:  float = 0.0
    force_verify: bool  = False
    last_updated: str   = ""

    def add_sample(self, conf: float, success: bool, strategy: str) -> None:
        """Thêm mẫu mới, recalculate stats."""
        self.samples.append(ConfSample(conf=conf, success=success, strategy=strategy))
        # Giữ _MAX_SAMPLES_PER_ELEMENT mẫu gần nhất
        if len(self.samples) > _MAX_SAMPLES_PER_ELEMENT:
            self.samples = self.samples[-_MAX_SAMPLES_PER_ELEMENT:]
        self._recalculate()
        self.last_updated = date.today().isoformat()

    def _recalculate(self) -> None:
        """Tính lại success_rate, avg_conf và trust_bonus từ samples."""
        n = len(self.samples)
        if n == 0:
            return
        successes = sum(1 for s in self.samples if s.success)
        self.success_rate = round(successes / n, 4)
        self.avg_conf     = round(sum(s.conf for s in self.samples) / n, 4)

        # Tính trust_bonus chỉ khi đủ mẫu
        if n < _MIN_SAMPLES_FOR_CALIBRATION:
            self.trust_bonus  = 0.0
            self.force_verify = False
            return

        # trust_bonus = f(success_rate, avg_conf, n)
        # Công thức: bonus tỉ lệ thuận với (success_rate - 0.85) × log(n)
        # Phạt khi success_rate thấp hơn avg_conf (VLM tự tin nhầm)
        sr_delta  = self.success_rate - 0.85   # >0 = tốt hơn baseline
        conf_bias = self.success_rate - self.avg_conf  # >0 = thực tế tốt hơn conf báo
        weight    = min(1.0, math.log(n + 1) / math.log(20))  # saturate at n=20

        raw_bonus = (sr_delta * 0.5 + conf_bias * 0.5) * weight
        self.trust_bonus = round(
            max(_MIN_TRUST_PENALTY, min(_MAX_TRUST_BONUS, raw_bonus)), 4
        )

        # force_verify khi VLM tự tin cao nhưng thực tế thất bại nhiều
        self.force_verify = (
            self.avg_conf >= 0.85
            and self.success_rate < 0.65
            and n >= 10
        )

    @property
    def sample_count(self) -> int:
        return len(self.samples)

    def to_dict(self) -> dict:
        return {
            "samples": [
                {"conf": s.conf, "success": s.success,
                 "strategy": s.strategy, "ts": round(s.ts)}
                for s in self.samples[-20:]  # chỉ lưu 20 mẫu gần nhất
            ],
            "success_rate": self.success_rate,
            "avg_conf":     self.avg_conf,
            "trust_bonus":  self.trust_bonus,
            "force_verify": self.force_verify,
            "sample_count": self.sample_count,
            "last_updated": self.last_updated,
        }

    @staticmethod
    def from_dict(domain: str, action_key: str, d: dict) -> "ElementHistory":
        h = ElementHistory(domain=domain, action_key=action_key)
        h.samples = [
            ConfSample(
                conf=float(s["conf"]), success=bool(s["success"]),
                strategy=s.get("strategy", ""), ts=float(s.get("ts", 0))
            )
            for s in d.get("samples", [])
        ]
        h.success_rate = float(d.get("success_rate", 1.0))
        h.avg_conf     = float(d.get("avg_conf", 0.8))
        h.trust_bonus  = float(d.get("trust_bonus", 0.0))
        h.force_verify = bool(d.get("force_verify", False))
        h.last_updated = d.get("last_updated", "")
        return h


# ══════════════════════════════════════════════════════════════════
# ConfidenceHistory
# ══════════════════════════════════════════════════════════════════

class ConfidenceHistory:
    """
    B2 Dynamic Confidence Calibration.

    Track per-(domain, action_key) confidence history.
    Trả về AdjustedConf với trust_bonus để giúp các element
    "đáng tin nhưng VLM underreport" vượt qua ConfidenceGate.

    KHÔNG thay đổi CONFIDENCE_THRESHOLD (R-21 invariant).

    Usage:
        ch = ConfidenceHistory()

        # Sau khi có raw conf từ strategy
        adjusted = ch.adjust("facebook.com", "fb_post_button", raw_conf=0.76)
        # → adjusted.confidence = 0.82 (pass gate!)
        # → adjusted.force_verify = False

        # Sau khi click + verify hoàn thành
        ch.record("facebook.com", "fb_post_button",
                  conf=0.76, success=True, strategy="zoom_fine")
    """

    def __init__(self, storage_path: str = "data/memory/confidence_history.json") -> None:
        self._path = Path(storage_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # {domain → {action_key → ElementHistory}}
        self._data: dict[str, dict[str, ElementHistory]] = {}
        self._load()

    # ── Public API ────────────────────────────────────────────────

    def adjust(
        self,
        domain: str,
        action_key: str,
        raw_conf: float,
    ) -> AdjustedConf:
        """
        Hiệu chỉnh confidence dựa trên lịch sử thực tế.

        Args:
            domain:     Domain trang (vd: "facebook.com")
            action_key: Action key (vd: "fb_post_button")
            raw_conf:   Confidence gốc từ VLM/strategy (0.0–1.0)

        Returns:
            AdjustedConf với .confidence đã hiệu chỉnh
        """
        if not domain or not action_key:
            return AdjustedConf(confidence=raw_conf, raw_conf=raw_conf,
                                trust_bonus=0.0, source="no_key")

        hist = self._get_or_none(domain, action_key)
        if hist is None or hist.sample_count < _MIN_SAMPLES_FOR_CALIBRATION:
            return AdjustedConf(
                confidence=raw_conf, raw_conf=raw_conf, trust_bonus=0.0,
                source="insufficient_data",
                sample_count=hist.sample_count if hist else 0,
            )

        adjusted = max(0.0, min(1.0, raw_conf + hist.trust_bonus))
        source = (
            f"trust_bonus={hist.trust_bonus:+.3f} "
            f"(sr={hist.success_rate:.0%}, n={hist.sample_count})"
        )

        if hist.trust_bonus != 0.0:
            icon = "📈" if hist.trust_bonus > 0 else "📉"
            _vlog(icon, f"B2 ConfCalib [{domain}:{action_key}] "
                        f"{raw_conf:.3f} → {adjusted:.3f} {source}")

        return AdjustedConf(
            confidence=round(adjusted, 4),
            raw_conf=raw_conf,
            trust_bonus=hist.trust_bonus,
            force_verify=hist.force_verify,
            source=source,
            sample_count=hist.sample_count,
        )

    def record(
        self,
        domain: str,
        action_key: str,
        conf: float,
        success: bool,
        strategy: str = "",
    ) -> None:
        """
        Ghi nhận 1 lần click — thêm mẫu và recalculate.

        Args:
            domain:     Domain trang
            action_key: Action key
            conf:       Raw confidence từ strategy
            success:    Click + verify có thành công không
            strategy:   Strategy đã dùng (để phân tích sau)
        """
        if not domain or not action_key:
            return

        if domain not in self._data:
            self._data[domain] = {}
        if action_key not in self._data[domain]:
            self._data[domain][action_key] = ElementHistory(
                domain=domain, action_key=action_key
            )

        self._data[domain][action_key].add_sample(conf, success, strategy)
        self._save_async()

    def get_stats(self, domain: str = "", action_key: str = "") -> dict:
        """Stats tổng hợp cho Admin Panel."""
        if domain and action_key:
            hist = self._get_or_none(domain, action_key)
            if hist:
                return hist.to_dict()
            return {}

        total = sum(len(keys) for keys in self._data.values())
        calibrated = sum(
            1 for keys in self._data.values()
            for h in keys.values()
            if abs(h.trust_bonus) > 0.01
        )
        return {
            "total_elements": total,
            "calibrated":     calibrated,
            "domains":        list(self._data.keys()),
        }

    # ── Helpers ───────────────────────────────────────────────────

    def _get_or_none(self, domain: str, action_key: str) -> Optional[ElementHistory]:
        return self._data.get(domain, {}).get(action_key)

    # ── Persistence ───────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            # B3: Load bootstrap data for cold start
            self._load_bootstrap()
            return
        try:
            raw = json.loads(self._path.read_text("utf-8"))
            for domain, keys in raw.items():
                self._data[domain] = {}
                for ak, d in keys.items():
                    self._data[domain][ak] = ElementHistory.from_dict(domain, ak, d)
            n = sum(len(v) for v in self._data.values())
            _vlog("📊", f"ConfidenceHistory loaded: {n} elements across {len(self._data)} domains")
        except Exception as exc:
            _vlog("⚠️", f"ConfidenceHistory load error: {exc}")

    def _load_bootstrap(self) -> None:
        """B3 v2.4: Load pre-built confidence data for top 20 websites on cold start."""
        # FIX #20: Use absolute path based on project root, not cwd
        _project_root = Path(__file__).resolve().parent.parent
        bootstrap_path = _project_root / "data" / "memory" / "confidence_bootstrap.json"
        if not bootstrap_path.exists():
            # Fallback: try relative (works when cwd is project root)
            bootstrap_path = Path("data/memory/confidence_bootstrap.json")
        if not bootstrap_path.exists():
            return
        try:
            raw = json.loads(bootstrap_path.read_text("utf-8"))
            count = 0
            for domain, elements in raw.items():
                if domain.startswith("_"):  # skip _meta
                    continue
                if domain not in self._data:
                    self._data[domain] = {}
                for action_key, stats in elements.items():
                    if action_key in self._data[domain]:
                        continue  # don't override existing real data
                    # Create synthetic ElementHistory from bootstrap stats
                    sample_count = stats.get("samples", 10)
                    success_rate = stats.get("success_rate", 0.8)
                    avg_conf = stats.get("avg_conf", 0.75)
                    successes = int(sample_count * success_rate)
                    self._data[domain][action_key] = ElementHistory(
                        domain=domain,
                        action_key=action_key,
                        sample_count=sample_count,
                        success_count=successes,
                        total_confidence=avg_conf * sample_count,
                        last_success_conf=avg_conf,
                        last_updated="bootstrap",  # FIX BUG#4: str not float
                    )
                    count += 1
            if count > 0:
                _vlog("🚀", f"ConfidenceHistory bootstrap: {count} elements from {len(self._data)} domains (cold-start eliminated)")
        except Exception as exc:
            _vlog("⚠️", f"Bootstrap load error: {exc}")

    def _save(self) -> None:
        try:
            raw = {
                domain: {ak: h.to_dict() for ak, h in keys.items()}
                for domain, keys in self._data.items()
            }
            self._path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), "utf-8")
        except Exception as exc:
            _vlog("⚠️", f"ConfidenceHistory save error: {exc}")

    def _save_async(self) -> None:
        import threading
        threading.Thread(target=self._save, daemon=True).start()


# ── Singleton ─────────────────────────────────────────────────────
_instance: ConfidenceHistory | None = None


def get_confidence_history() -> ConfidenceHistory:
    global _instance
    if _instance is None:
        _instance = ConfidenceHistory()
    return _instance
