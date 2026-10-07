# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/proactive_screen.py — Phidipus B4 Proactive Screen Understanding v2.2
══════════════════════════════════════════════════════════════════════════════

B4: Proactive Screen Understanding — Hiểu màn hình TRƯỚC khi cần.

Vấn đề:
  Workflow hiện tại: mỗi node vision_click hỏi VLM riêng lẻ:
    Node 1: "Đây là nút Upload không?" → 1 VLM call (3s)
    Node 2: "Đây là ô textarea không?" → 1 VLM call (3s)
    Node 3: "Đây là nút Post không?" → 1 VLM call (3s)
    → Tổng 9s chỉ cho 3 bước đầu.

  Nguyên nhân gốc: mỗi node hoạt động độc lập, không biết "bức tranh toàn cảnh".

Giải pháp — ProactiveScreenUnderstanding:
  Khi workflow bắt đầu (hoặc sau page load), chạy 1 VLM call DUY NHẤT:

  Screen Inventory (1 VLM call):
    → Phát hiện TẤT CẢ interactive elements có thể click
    → Phát hiện dialogs/modals đang mở
    → Phát hiện trạng thái loading/ready
    → Cache kết quả cho toàn bộ workflow

  Kết quả:
    Node 1: lookup inventory["upload_button"]  → (x=450, y=320, conf=0.9) → S0 click!
    Node 2: lookup inventory["text_area"]      → (x=200, y=400, conf=0.95) → S0 click!
    Node 3: lookup inventory["post_button"]    → (x=820, y=650, conf=0.88) → S0 click!
    → Tổng <100ms cho 3 bước (thay vì 9s)

Architecture:
  workflow_executor, TRƯỚC vòng lặp node chính:
    psu = get_proactive_screen()
    screenshot = await capture()
    inventory = await psu.run_inventory(screenshot, vlm_fn)
    _vlog("📋", f"Screen inventory: {len(inventory.elements)} elements found")

  Mỗi node vision_click:
    cached = psu.get_element(node.description, domain=domain)
    if cached and cached.confidence >= threshold:
        → click(cached.x, cached.y)   # S0 — zero VLM
    else:
        → pipeline S1→S4 bình thường

  Invalidation (khi nào inventory cũ?):
    - Page navigation detected (URL change)
    - Major frame diff (> 30% pixel change)
    - Explicit psu.invalidate() call
    - TTL hết hạn (default 120s)

Integration points:
  1. workflow_executor.run() — gọi psu.run_inventory() trước khi bắt đầu
  2. vision_actor.find_and_click_v2() — thêm strategy S0b: inventory lookup
  3. workflow_executor._exec_vision_click() — thêm inventory check đầu tiên

Security invariants:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  R-21  Inventory elements có confidence field — vẫn pass qua ConfidenceGate
        trước khi dispatch IPC (không bypass R-21).

Process: orchestrator (L1)
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ── Constants ─────────────────────────────────────────────────────

# TTL của inventory cache (giây) — tự động invalidate sau thời gian này
_INVENTORY_TTL_S: float = 120.0

# Confidence tối thiểu để trả về từ inventory
_MIN_INVENTORY_CONF: float = 0.70

# Timeout VLM cho inventory (cho phép dài hơn vì phân tích toàn màn hình)
_INVENTORY_VLM_TIMEOUT: float = 20.0

# Số element tối đa lưu trong inventory (tránh quá lớn)
_MAX_INVENTORY_ELEMENTS: int = 30

# Fuzzy match threshold (0.0–1.0) cho get_element()
_FUZZY_MATCH_THRESHOLD: float = 0.6

# Prompt chuẩn cho inventory scan
_INVENTORY_PROMPT = """
Analyze this screenshot and identify ALL interactive UI elements.
For EACH element, return a JSON object with:
  - label: short descriptive name (e.g. "Post button", "Caption text area", "Upload photo button")
  - type: one of [button, text_field, textarea, link, icon, checkbox, radio, select, dialog, modal, image, tab, menu]
  - x: center x coordinate (integer pixels)
  - y: center y coordinate (integer pixels)
  - w: width in pixels (integer)
  - h: height in pixels (integer)
  - confidence: float 0.0-1.0 (how sure you are this element exists)
  - is_interactive: true if user can click/type on it, false otherwise
  - state: one of [enabled, disabled, loading, checked, unchecked, visible]
  - aria_label: accessible name if visible (empty string if not)

Also identify:
  - dialogs: list of any modal/dialog/popup elements
  - page_state: "loading" | "ready" | "error" | "partial"
  - focused_element: label of currently focused element (empty string if none)

Return ONLY a valid JSON object with this structure:
{
  "elements": [...],
  "dialogs": [...],
  "page_state": "ready",
  "focused_element": ""
}
No markdown, no explanation, just the JSON.
""".strip()

# Prompt ngắn hơn cho quick re-scan sau action
_QUICK_SCAN_PROMPT = """
Analyze this screenshot. List all CLICKABLE elements (buttons, links, interactive controls).
Return JSON: {"elements": [{"label": "...", "type": "button", "x": 0, "y": 0, "w": 0, "h": 0, "confidence": 0.9, "is_interactive": true, "state": "enabled", "aria_label": ""}], "page_state": "ready"}
ONLY JSON, no explanation.
""".strip()


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class InventoryElement:
    """
    1 UI element trong screen inventory.
    Tọa độ tuyệt đối (pixels) trên màn hình.
    """
    label:         str               # Tên mô tả: "Post button", "Caption textarea"
    elem_type:     str               # "button" | "text_field" | "dialog" | ...
    x:             int               # Center x (pixels)
    y:             int               # Center y (pixels)
    w:             int               # Width (pixels)
    h:             int               # Height (pixels)
    confidence:    float             # 0.0–1.0
    is_interactive: bool = True
    state:         str   = "enabled" # "enabled" | "disabled" | "loading" | ...
    aria_label:    str   = ""
    match_score:   float = 0.0       # điểm fuzzy match khi lookup (set by get_element)

    @property
    def center(self) -> tuple[int, int]:
        return (self.x, self.y)

    @property
    def is_reliable(self) -> bool:
        return self.confidence >= _MIN_INVENTORY_CONF and self.is_interactive

    @property
    def bounds(self) -> dict:
        return {"x": self.x - self.w//2, "y": self.y - self.h//2,
                "w": self.w, "h": self.h}

    def to_dict(self) -> dict:
        return {
            "label":          self.label,
            "type":           self.elem_type,
            "x":              self.x,
            "y":              self.y,
            "w":              self.w,
            "h":              self.h,
            "confidence":     self.confidence,
            "is_interactive": self.is_interactive,
            "state":          self.state,
            "aria_label":     self.aria_label,
        }


@dataclass
class ScreenInventory:
    """
    Kết quả scan toàn màn hình — cache cho workflow.
    """
    elements:        list[InventoryElement]    # Tất cả elements
    dialogs:         list[InventoryElement]    # Chỉ dialogs/modals
    page_state:      str                       # "loading" | "ready" | "error" | "partial"
    focused_element: str                       # Tên element đang focused
    captured_at:     float = field(default_factory=time.time)
    scan_ms:         int   = 0                 # Thời gian scan (ms)
    used_quick_scan: bool  = False             # True nếu dùng quick prompt
    url:             str   = ""                # URL tại thời điểm scan (nếu có)
    screenshot_hash: str   = ""               # Hash để detect khi nào cần rescan

    @property
    def age_s(self) -> float:
        return time.time() - self.captured_at

    @property
    def is_expired(self) -> bool:
        return self.age_s > _INVENTORY_TTL_S

    @property
    def is_ready(self) -> bool:
        return self.page_state in ("ready", "partial")

    @property
    def interactive_elements(self) -> list[InventoryElement]:
        return [e for e in self.elements if e.is_interactive and e.state == "enabled"]

    def summary(self) -> str:
        total     = len(self.elements)
        interact  = len(self.interactive_elements)
        dialog_ct = len(self.dialogs)
        state_str = f" | ⚠️ {dialog_ct} dialog(s)" if dialog_ct else ""
        return (f"{total} elements ({interact} interactive) | "
                f"page={self.page_state}{state_str} | "
                f"age={self.age_s:.0f}s | scan={self.scan_ms}ms")


# ══════════════════════════════════════════════════════════════════
# Core class
# ══════════════════════════════════════════════════════════════════

class ProactiveScreenUnderstanding:
    """
    B4 Proactive Screen Understanding.

    Chạy 1 VLM call toàn màn hình khi workflow bắt đầu,
    cache kết quả để các node tiếp theo lookup không cần VLM.

    Chiến lược tích hợp với strategies hiện có:
      S0a (ClickMemory)        → unchanged
      S0b (ScreenInventory)    → NEW — lookup từ inventory cache
      S1  (DOM/JS)             → unchanged
      S2–S4 (Vision pipeline)  → unchanged, chỉ chạy khi S0a+S0b miss
    """

    def __init__(
        self,
        vlm_fn: Optional[Callable[..., Awaitable[str]]] = None,
        gemini_api_key: str = "",
    ) -> None:
        self._vlm_fn     = vlm_fn
        self._gemini_key = gemini_api_key
        self._inventory: Optional[ScreenInventory] = None
        self._scan_lock  = asyncio.Lock()   # Tránh concurrent scans

    # ── Main API ─────────────────────────────────────────────────

    async def run_inventory(
        self,
        screenshot_bytes: bytes,
        vlm_fn: Optional[Callable] = None,
        url: str = "",
        quick: bool = False,
    ) -> ScreenInventory:
        """
        Chạy screen inventory scan.

        Args:
            screenshot_bytes: PNG/JPEG bytes của screenshot hiện tại
            vlm_fn:           override VLM function (ưu tiên hơn self._vlm_fn)
            url:              URL hiện tại (để log + invalidation)
            quick:            True = dùng prompt ngắn hơn (ít chi tiết hơn nhưng nhanh hơn)

        Returns:
            ScreenInventory — cũng lưu vào cache nội bộ
        """
        async with self._scan_lock:
            t0 = time.time()
            _vlog("🔍", f"Running screen inventory... (quick={quick})")

            # Chọn VLM
            _vlm = vlm_fn or self._vlm_fn
            prompt = _QUICK_SCAN_PROMPT if quick else _INVENTORY_PROMPT

            # Gọi VLM
            raw_response = ""
            try:
                if _vlm is not None:
                    raw_response = await asyncio.wait_for(
                        _vlm(screenshot_bytes, prompt),
                        timeout=_INVENTORY_VLM_TIMEOUT,
                    )
                elif self._gemini_key:
                    raw_response = await self._call_gemini(screenshot_bytes, prompt)
                else:
                    _vlog("⚠️", "Không có VLM — inventory trống")
                    return self._empty_inventory(t0)
            except asyncio.TimeoutError:
                _vlog("⏱️", f"Inventory VLM timeout ({_INVENTORY_VLM_TIMEOUT}s)")
                return self._empty_inventory(t0)
            except Exception as e:
                _vlog("⚠️", f"Inventory VLM error: {e}")
                return self._empty_inventory(t0)

            # Parse response
            inventory = _parse_inventory_response(raw_response, t0, url, quick)
            scan_ms = int((time.time() - t0) * 1000)
            inventory.scan_ms = scan_ms

            # Hash screenshot để detect khi nào cần rescan
            import hashlib
            inventory.screenshot_hash = hashlib.sha256(screenshot_bytes[:4096]).hexdigest()[:16]

            self._inventory = inventory
            _vlog("📋", f"Inventory done: {inventory.summary()} [{scan_ms}ms]")

            # Log từng element (debug)
            for e in inventory.interactive_elements[:8]:
                _vlog("  └", f"{e.elem_type:12s} '{e.label[:30]}' @ ({e.x},{e.y}) conf={e.confidence:.0%}")

            return inventory

    def get_element(
        self,
        description:    str,
        elem_type:      Optional[str] = None,
        min_confidence: float = _MIN_INVENTORY_CONF,
    ) -> Optional[InventoryElement]:
        """
        Tìm element trong inventory theo mô tả.
        Dùng fuzzy matching — không cần khớp chính xác.

        Args:
            description:    mô tả tự nhiên ("Post button", "Upload photo")
            elem_type:      lọc theo type nếu có ("button", "text_field"...)
            min_confidence: confidence tối thiểu (default 0.70)

        Returns:
            InventoryElement nếu tìm thấy, None nếu không

        Example:
            elem = psu.get_element("submit post button")
            → tìm trong inventory elements có label gần nhất với "submit post button"
        """
        if self._inventory is None or self._inventory.is_expired:
            return None

        candidates = self._inventory.elements
        if elem_type:
            candidates = [e for e in candidates if e.elem_type == elem_type]

        if not candidates:
            return None

        # Fuzzy match: tính score cho từng element
        desc_words = set(_tokenize(description.lower()))
        best: Optional[InventoryElement] = None
        best_score = 0.0

        for elem in candidates:
            if elem.confidence < min_confidence:
                continue
            if not elem.is_interactive:
                continue
            if elem.state == "disabled":
                continue

            # Score dựa trên word overlap
            label_words = set(_tokenize(elem.label.lower()))
            aria_words  = set(_tokenize(elem.aria_label.lower())) if elem.aria_label else set()
            all_words   = label_words | aria_words

            if not desc_words or not all_words:
                continue

            intersection = desc_words & all_words
            union        = desc_words | all_words
            jaccard      = len(intersection) / len(union) if union else 0.0

            # Bonus nếu tất cả từ key trong description đều có trong label
            key_words = {w for w in desc_words if len(w) > 3}
            key_match = len(key_words & all_words) / len(key_words) if key_words else 0.0

            score = 0.6 * jaccard + 0.4 * key_match

            # Bonus nếu type hint khớp
            if elem_type and elem.elem_type == elem_type:
                score += 0.1

            if score > best_score:
                best_score = score
                best = elem

        if best and best_score >= _FUZZY_MATCH_THRESHOLD:
            result = InventoryElement(
                label=best.label,
                elem_type=best.elem_type,
                x=best.x, y=best.y, w=best.w, h=best.h,
                confidence=best.confidence,
                is_interactive=best.is_interactive,
                state=best.state,
                aria_label=best.aria_label,
                match_score=best_score,
            )
            _vlog("✅", f"Inventory hit: '{description}' → '{best.label}' "
                        f"@ ({best.x},{best.y}) match={best_score:.0%}")
            return result

        _vlog("🔍", f"Inventory miss: '{description}' (best_score={best_score:.0%})")
        return None

    def get_by_type(self, elem_type: str) -> list[InventoryElement]:
        """Lấy tất cả elements của 1 type."""
        if self._inventory is None or self._inventory.is_expired:
            return []
        return [e for e in self._inventory.elements
                if e.elem_type == elem_type and e.confidence >= _MIN_INVENTORY_CONF]

    def is_page_ready(self) -> bool:
        """
        Kiểm tra xem page đã load xong chưa.
        Dùng trước khi bắt đầu workflow actions.
        """
        if self._inventory is None:
            return False
        if self._inventory.is_expired:
            return False
        return self._inventory.is_ready

    def has_dialog(self) -> bool:
        """True nếu đang có dialog/modal đang mở."""
        if self._inventory is None or self._inventory.is_expired:
            return False
        return len(self._inventory.dialogs) > 0

    def get_dialogs(self) -> list[InventoryElement]:
        """Trả về danh sách dialogs hiện tại."""
        if self._inventory is None or self._inventory.is_expired:
            return []
        return list(self._inventory.dialogs)

    def get_current_inventory(self) -> Optional[ScreenInventory]:
        """Lấy inventory hiện tại (có thể đã expired)."""
        return self._inventory

    def invalidate(self, reason: str = "") -> None:
        """
        Xóa inventory cache.
        Gọi khi: page navigation, major UI change, sau dialog close.
        """
        if self._inventory:
            age = self._inventory.age_s
            _vlog("🗑️", f"Inventory invalidated after {age:.0f}s "
                         f"({reason or 'manual'})")
        self._inventory = None

    def should_rescan(
        self,
        screenshot_bytes: Optional[bytes] = None,
        url: str = "",
    ) -> bool:
        """
        Kiểm tra xem có cần chạy inventory scan mới không.

        Returns True khi:
          - Chưa có inventory
          - Inventory đã expired (TTL hết)
          - URL thay đổi
          - Screenshot hash khác nhau (major page change)
        """
        if self._inventory is None:
            return True
        if self._inventory.is_expired:
            _vlog("⏱️", f"Inventory expired (age={self._inventory.age_s:.0f}s)")
            return True
        if url and self._inventory.url and url != self._inventory.url:
            _vlog("🌐", f"URL changed: {self._inventory.url} → {url}")
            return True
        if screenshot_bytes:
            import hashlib
            new_hash = hashlib.sha256(screenshot_bytes[:4096]).hexdigest()[:16]
            if self._inventory.screenshot_hash and new_hash != self._inventory.screenshot_hash:
                # Chỉ rescan nếu hash khác — có thể page đã thay đổi đáng kể
                # (Không tự invalidate — để caller quyết định)
                return True
        return False

    # ── VLM Helpers ───────────────────────────────────────────────

    async def _call_gemini(self, screenshot_bytes: bytes, prompt: str) -> str:
        """Gọi Gemini Vision trực tiếp nếu không có vlm_fn."""
        try:
            from social.vision_actor import _call_gemini_vision
            return await _call_gemini_vision(
                screenshot_bytes,
                prompt,
                self._gemini_key,
                timeout=_INVENTORY_VLM_TIMEOUT,
            )
        except Exception as e:
            _vlog("⚠️", f"Gemini inventory call failed: {e}")
            raise

    def _empty_inventory(self, t0: float) -> ScreenInventory:
        """Trả về inventory trống khi VLM fail."""
        return ScreenInventory(
            elements=[],
            dialogs=[],
            page_state="unknown",
            focused_element="",
            captured_at=t0,
            scan_ms=int((time.time() - t0) * 1000),
        )


# ══════════════════════════════════════════════════════════════════
# Parsing helpers
# ══════════════════════════════════════════════════════════════════

def _parse_inventory_response(
    raw: str,
    t0: float,
    url: str,
    quick: bool,
) -> ScreenInventory:
    """
    Parse VLM response JSON thành ScreenInventory.
    Robust với malformed JSON, missing fields, extra text.
    """
    raw = raw.strip()

    # Strip markdown code fences nếu có
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)

    # Tìm JSON object đầu tiên trong response
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        _vlog("⚠️", f"Inventory parse: không tìm thấy JSON trong response")
        return ScreenInventory(
            elements=[], dialogs=[], page_state="unknown",
            focused_element="", captured_at=t0, url=url,
        )

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError as e:
        _vlog("⚠️", f"Inventory JSON parse error: {e}")
        return ScreenInventory(
            elements=[], dialogs=[], page_state="unknown",
            focused_element="", captured_at=t0, url=url,
        )

    # Parse elements
    raw_elements: list[dict] = data.get("elements", [])
    elements: list[InventoryElement] = []
    for raw_e in raw_elements[:_MAX_INVENTORY_ELEMENTS]:
        try:
            elem = _parse_element(raw_e)
            if elem:
                elements.append(elem)
        except Exception:
            continue

    # Parse dialogs (có thể là list hoặc trong elements với type=dialog)
    raw_dialogs: list[dict] = data.get("dialogs", [])
    dialogs: list[InventoryElement] = []
    for raw_d in raw_dialogs:
        try:
            elem = _parse_element(raw_d)
            if elem:
                dialogs.append(elem)
        except Exception:
            continue

    # Thêm dialog từ elements list
    for e in elements:
        if e.elem_type in ("dialog", "modal") and e not in dialogs:
            dialogs.append(e)

    page_state     = str(data.get("page_state", "unknown"))
    focused_elem   = str(data.get("focused_element", ""))

    return ScreenInventory(
        elements=elements,
        dialogs=dialogs,
        page_state=page_state,
        focused_element=focused_elem,
        captured_at=t0,
        url=url,
        used_quick_scan=quick,
    )


def _parse_element(d: dict) -> Optional[InventoryElement]:
    """Parse 1 element dict → InventoryElement. Returns None nếu invalid."""
    if not isinstance(d, dict):
        return None

    label = str(d.get("label", d.get("name", ""))).strip()
    if not label:
        return None

    x = int(d.get("x", 0))
    y = int(d.get("y", 0))
    w = int(d.get("w", d.get("width", 50)))
    h = int(d.get("h", d.get("height", 30)))

    if x < 0 or y < 0 or w <= 0 or h <= 0:
        return None  # Invalid coordinates

    confidence = float(d.get("confidence", 0.75))
    confidence = max(0.0, min(1.0, confidence))

    return InventoryElement(
        label=label,
        elem_type=str(d.get("type", d.get("elem_type", "button"))).lower(),
        x=x, y=y, w=w, h=h,
        confidence=confidence,
        is_interactive=bool(d.get("is_interactive", True)),
        state=str(d.get("state", "enabled")).lower(),
        aria_label=str(d.get("aria_label", "")),
    )


def _tokenize(text: str) -> list[str]:
    """Tách text thành tokens để fuzzy match."""
    # Loại bỏ ký tự đặc biệt, split theo space
    clean = re.sub(r"[^\w\s]", " ", text)
    return [w for w in clean.lower().split() if len(w) >= 2]


# ══════════════════════════════════════════════════════════════════
# Module-level singleton
# ══════════════════════════════════════════════════════════════════

_INSTANCE: Optional[ProactiveScreenUnderstanding] = None


def get_proactive_screen(
    vlm_fn: Optional[Callable] = None,
    gemini_api_key: str = "",
) -> ProactiveScreenUnderstanding:
    """
    Lấy singleton ProactiveScreenUnderstanding.

    Dùng chung 1 instance trong process để tái sử dụng inventory cache.
    Thread-safe (GIL bảo vệ assignment).
    """
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = ProactiveScreenUnderstanding(
            vlm_fn=vlm_fn,
            gemini_api_key=gemini_api_key,
        )
        _vlog("🚀", "ProactiveScreenUnderstanding singleton initialized")
    elif vlm_fn is not None and _INSTANCE._vlm_fn is None:
        _INSTANCE._vlm_fn = vlm_fn
    elif gemini_api_key and not _INSTANCE._gemini_key:
        _INSTANCE._gemini_key = gemini_api_key
    return _INSTANCE


def reset_proactive_screen() -> None:
    """Reset singleton — dùng trong test hoặc khi cần fresh instance."""
    global _INSTANCE
    _INSTANCE = None


# ══════════════════════════════════════════════════════════════════
# Convenience helpers cho workflow_executor
# ══════════════════════════════════════════════════════════════════

async def proactive_inventory_hook(
    screenshot_bytes: bytes,
    vlm_fn: Optional[Callable] = None,
    gemini_api_key: str = "",
    url: str = "",
    quick: bool = False,
) -> ScreenInventory:
    """
    Hook dùng trong workflow_executor trước vòng lặp node.

    Ví dụ:
        inventory = await proactive_inventory_hook(
            screenshot_bytes=snap.bytes_raw,
            gemini_api_key=self._gemini_key,
            url=current_url,
        )
        _vlog("📋", f"Screen inventory: {inventory.summary()}")
    """
    psu = get_proactive_screen(vlm_fn=vlm_fn, gemini_api_key=gemini_api_key)
    if psu.should_rescan(screenshot_bytes, url):
        return await psu.run_inventory(
            screenshot_bytes, vlm_fn=vlm_fn, url=url, quick=quick
        )
    # Inventory còn hạn — dùng lại
    inv = psu.get_current_inventory()
    if inv:
        _vlog("⚡", f"Reusing inventory (age={inv.age_s:.0f}s): {inv.summary()}")
        return inv
    return await psu.run_inventory(screenshot_bytes, vlm_fn=vlm_fn, url=url, quick=quick)


def inventory_lookup(
    description: str,
    elem_type: Optional[str] = None,
) -> Optional[InventoryElement]:
    """
    Quick lookup từ inventory — dùng trong vision_actor như strategy S0b.

    Ví dụ trong find_and_click_v2():
        # S0b: ProactiveScreen inventory
        cached = inventory_lookup("Post button", elem_type="button")
        if cached and cached.confidence >= threshold:
            return {"success": True, "x": cached.x, "y": cached.y,
                    "confidence": cached.confidence, "strategy": "S0b_inventory"}
    """
    psu = get_proactive_screen()
    return psu.get_element(description, elem_type=elem_type)


# ══════════════════════════════════════════════════════════════════
# CHANGELOG
# ══════════════════════════════════════════════════════════════════
#
# v2.2 — 2026-03-25
#   - Initial implementation of B4 Proactive Screen Understanding
#   - InventoryElement: full UI element descriptor
#   - ScreenInventory: cached full-page element map
#   - ProactiveScreenUnderstanding.run_inventory() — 1 VLM call lấy tất cả
#   - get_element() — fuzzy matching (Jaccard + key word coverage)
#   - get_by_type() — filter by elem_type
#   - is_page_ready(), has_dialog(), get_dialogs() — state checks
#   - should_rescan() — intelligent cache invalidation
#   - invalidate() — explicit cache clear
#   - _parse_inventory_response() — robust JSON parser (handles markdown fences)
#   - Module singleton get_proactive_screen()
#   - proactive_inventory_hook() — convenience wrapper cho workflow_executor
#   - inventory_lookup() — S0b strategy hook cho vision_actor
#   - Async scan lock — tránh concurrent VLM calls
#   - TTL 120s — auto-expire inventory sau 2 phút
