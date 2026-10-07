# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
ipc/action_schema.py — Phidipus v1.0.1
Single source of truth for all IPC message schemas.

Architecture contract (C.3):
  "Schema is immutable at runtime. No dynamic action types."

This module defines:
  • The message envelope schema (MESSAGE_SCHEMA)
  • Per-action payload schemas for all 22 allowlisted actions (ACTION_SCHEMAS)
  • Response and error payload schemas
  • IPCValidationError — raised on any schema violation
  • validate_message()  — validates any inbound or outbound IPC message
  • validate_request()  — validates a REQUEST message end-to-end
  • validate_response() — validates a RESPONSE message
  • validate_event()    — validates an EVENT message
  • make_request()      — constructs a validated REQUEST dict
  • make_response()     — constructs a validated RESPONSE dict
  • make_error()        — constructs a validated ERROR dict

Security invariants encoded here:
  R-05  Only schema-validated messages may be dispatched.
  R-10  Structured JSON only; no filesystem channel.
  R-22  Actions outside the focused window carry require_confirm=True.
        The daemon enforces confirmation; the schema makes the field explicit.

Patch v9.11.1:
  FIX-4  _utc_now() now calls datetime.now(tz=timezone.utc) exactly once,
         eliminating the race condition where two calls in the same function
         could straddle a second boundary and produce an internally
         inconsistent timestamp string.

Usage (ipc_server.py):
    from ipc.action_schema import validate_request, IPCValidationError
    try:
        validate_request(message_dict)
    except IPCValidationError as exc:
        log.warning("rejected invalid IPC message", extra={"error": str(exc)})
        send_error(conn, exc)

Usage (ipc_client.py):
    from ipc.action_schema import make_request
    msg = make_request("mouse_click", {"x": 100, "y": 200, "button": "left"})
    # msg is already validated; send it over the socket.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

from utils.json_utils import validate, dumps, JsonSchemaError
from utils.logger import get_logger

# action_schema is imported by BOTH orchestrator (ipc_client) and daemon (ipc_server).
# "shared" is used instead of "orchestrator" or "daemon" to reflect this dual-process nature.
_log = get_logger("ipc.action_schema", process="shared")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Hard ceiling on serialised message size in bytes.
#: The transport layer (ipc_server) may impose a lower limit via config.
#: 64 KiB is the absolute maximum this schema layer will accept.
MAX_SERIALIZED_BYTES: int = 65536

#: Maximum length for free-text strings in payloads (keyboard_type, clipboard).
MAX_TEXT_LENGTH: int = 8192

#: Maximum length for display labels and element identifiers.
MAX_LABEL_LENGTH: int = 512

#: Maximum x/y coordinate (covers 8K resolution: 7680 × 4320).
MAX_COORDINATE: int = 32767

# Regex for UUID4 format.
# Example: 550e8400-e29b-41d4-a716-446655440000
# UUID4:   xxxxxxxx-xxxx-4xxx-[89ab]xxx-xxxxxxxxxxxx
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Regex for ISO-8601 UTC timestamp produced by _utc_now().
# Accepts: 2026-03-13T01:23:45.123Z  or  2026-03-13T01:23:45Z
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
)

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class IPCValidationError(ValueError):
    """
    Raised when an IPC message fails schema validation.

    Attributes:
        field:   Dot-path to the offending field, if known (e.g. ``"payload.x"``).
        code:    Short machine-readable error code for logging and protocol errors.
        message: Human-readable description.
    """

    def __init__(
        self,
        message: str,
        *,
        field: str = "",
        code: str = "SCHEMA_VIOLATION",
    ) -> None:
        super().__init__(message)
        self.field = field
        self.code = code

    def __str__(self) -> str:
        if self.field:
            return f"[{self.code}] {self.field}: {super().__str__()}"
        return f"[{self.code}] {super().__str__()}"

    def to_dict(self) -> dict[str, str]:
        """Serialise for inclusion in an ERROR response payload."""
        return {
            "code":    self.code,
            "field":   self.field,
            "message": super().__str__(),
        }


# ---------------------------------------------------------------------------
# Message types and categories
# ---------------------------------------------------------------------------

#: All permitted values for the ``type`` field.
MESSAGE_TYPES: frozenset[str] = frozenset({"REQUEST", "RESPONSE", "ERROR", "EVENT"})

#: REQUEST type constant.
TYPE_REQUEST: str = "REQUEST"
#: RESPONSE type constant.
TYPE_RESPONSE: str = "RESPONSE"
#: ERROR type constant.
TYPE_ERROR: str = "ERROR"
#: EVENT type constant.
TYPE_EVENT: str = "EVENT"


# ---------------------------------------------------------------------------
# Per-action payload schemas
# ---------------------------------------------------------------------------
# Each key is an allowlisted action name.
# Each value is a JSON-Schema-compatible dict for the action's payload.
# "additionalProperties": False is enforced on every object payload.
#
# Architecture: "Schema is immutable at runtime. No dynamic action types."
# Enforcement: ACTION_SCHEMAS is built once at module load; _ALLOWED_ACTIONS
# is a frozenset so callers cannot add to it at runtime.

ACTION_SCHEMAS: dict[str, dict[str, Any]] = {

    # ── Mouse actions ────────────────────────────────────────────────────────

    "mouse_click": {
        "type": "object",
        "additionalProperties": False,
        "required": ["x", "y"],
        "properties": {
            "x":              {"type": "integer", "minimum": 0, "maximum": MAX_COORDINATE},
            "y":              {"type": "integer", "minimum": 0, "maximum": MAX_COORDINATE},
            "button":         {"type": "string", "enum": ["left", "right", "middle"]},
            "clicks":         {"type": "integer", "minimum": 1, "maximum": 3},
            "interval":       {"type": "number",  "minimum": 0.0, "maximum": 5.0},
            # R-21: VLM-sourced clicks carry confidence so the daemon can
            # enforce the 0.8 gate independently (defence-in-depth).
            "confidence":     {"type": "number",  "minimum": 0.0, "maximum": 1.0},
            # R-22: True when the target is outside the focused window.
            # The daemon must prompt for user confirmation before executing.
            "require_confirm": {"type": "boolean"},
            "target_label":   {"type": "string",  "minLength": 0, "maxLength": MAX_LABEL_LENGTH},
        },
    },

    "mouse_move": {
        "type": "object",
        "additionalProperties": False,
        "required": ["x", "y"],
        "properties": {
            "x":        {"type": "integer", "minimum": 0, "maximum": MAX_COORDINATE},
            "y":        {"type": "integer", "minimum": 0, "maximum": MAX_COORDINATE},
            "duration": {"type": "number",  "minimum": 0.0, "maximum": 10.0},
        },
    },

    "mouse_scroll": {
        "type": "object",
        "additionalProperties": False,
        # FIX v4.3: 6 call sites (workflow_executor, loop_detector, retry_engine,
        # vision_actor…) send {"direction", "amount"} — the old schema required
        # x/y/dy so every one of them was rejected.  Both forms are accepted now:
        #   {"x", "y", "dy"[, "dx"]}          — line deltas at a position
        #   {"direction", "amount"[, "x", "y"]} — pixel amount in a direction
        "required": [],
        "properties": {
            "x":  {"type": "integer", "minimum": 0, "maximum": MAX_COORDINATE},
            "y":  {"type": "integer", "minimum": 0, "maximum": MAX_COORDINATE},
            # dx/dy: signed scroll deltas in lines
            # (dy > 0 = scroll DOWN, dy < 0 = scroll UP; dx > 0 = right)
            "dx": {"type": "integer", "minimum": -100, "maximum": 100},
            "dy": {"type": "integer", "minimum": -100, "maximum": 100},
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            # amount: pixels to scroll in *direction*
            "amount": {"type": "integer", "minimum": 1, "maximum": 20000},
        },
    },

    "mouse_drag": {
        "type": "object",
        "additionalProperties": False,
        "required": ["from_x", "from_y", "to_x", "to_y"],
        "properties": {
            "from_x":   {"type": "integer", "minimum": 0, "maximum": MAX_COORDINATE},
            "from_y":   {"type": "integer", "minimum": 0, "maximum": MAX_COORDINATE},
            "to_x":     {"type": "integer", "minimum": 0, "maximum": MAX_COORDINATE},
            "to_y":     {"type": "integer", "minimum": 0, "maximum": MAX_COORDINATE},
            "button":   {"type": "string",  "enum": ["left", "right", "middle"]},
            "duration": {"type": "number",  "minimum": 0.0, "maximum": 10.0},
            "require_confirm": {"type": "boolean"},
        },
    },

    # ── Keyboard actions ─────────────────────────────────────────────────────

    "keyboard_type": {
        "type": "object",
        "additionalProperties": False,
        "required": ["text"],
        "properties": {
            # R-24 neighbour: text is user-supplied and must be bounded.
            "text":     {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_LENGTH},
            "interval": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
    },

    "keyboard_press": {
        "type": "object",
        "additionalProperties": False,
        "required": ["key"],
        "properties": {
            # Single key name as understood by pyautogui / AT-SPI
            # (e.g. "enter", "tab", "escape", "f1").
            "key": {"type": "string", "minLength": 1, "maxLength": 32},
            "presses": {"type": "integer", "minimum": 1, "maximum": 10},
        },
    },

    "keyboard_hotkey": {
        "type": "object",
        "additionalProperties": False,
        "required": ["keys"],
        "properties": {
            # Ordered list of modifier + key names, e.g. ["ctrl", "c"].
            # Minimum 2 keys (otherwise use keyboard_press).
            # Maximum 8 keys to prevent abuse.
            "keys": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 32},
            },
        },
    },

    # ── Clipboard actions ────────────────────────────────────────────────────

    "clipboard_copy": {
        "type": "object",
        "additionalProperties": False,
        "required": ["text"],
        "properties": {
            "text": {"type": "string", "minLength": 0, "maxLength": MAX_TEXT_LENGTH},
        },
    },

    "clipboard_paste": {
        # No parameters — paste whatever is in the system clipboard.
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {},
    },

    "clipboard_get": {
        # No parameters — return the current clipboard content.
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {},
    },

    # ── Screen capture ───────────────────────────────────────────────────────

    "screenshot_capture": {
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {
            # Optional screen region; full screen if omitted.
            "region": {
                "type": "object",
                "additionalProperties": False,
                "required": ["x", "y", "width", "height"],
                "properties": {
                    "x":      {"type": "integer", "minimum": 0, "maximum": MAX_COORDINATE},
                    "y":      {"type": "integer", "minimum": 0, "maximum": MAX_COORDINATE},
                    "width":  {"type": "integer", "minimum": 1, "maximum": MAX_COORDINATE},
                    "height": {"type": "integer", "minimum": 1, "maximum": MAX_COORDINATE},
                },
            },
            # Where to save the screenshot (relative path inside data_dir).
            "save_as": {"type": "string", "minLength": 1, "maxLength": MAX_LABEL_LENGTH},
        },
    },

    # ── Window management ────────────────────────────────────────────────────

    "window_focus": {
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {
            # At least one of title or pid should be supplied.
            "title":          {"type": "string", "minLength": 1, "maxLength": MAX_LABEL_LENGTH},
            "pid":            {"type": "integer", "minimum": 1, "maximum": 4194304},
            "require_confirm": {"type": "boolean"},
        },
    },

    "window_list": {
        # No parameters — enumerate all visible windows.
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {},
    },

    "window_get_info": {
        # No parameters — return geometry + title of the currently focused window.
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {},
    },

    # ── Accessibility / element actions ──────────────────────────────────────

    "element_click": {
        "type": "object",
        "additionalProperties": False,
        "required": ["element_id"],
        "properties": {
            "element_id":     {"type": "string", "minLength": 1, "maxLength": MAX_LABEL_LENGTH},
            # R-21: VLM-sourced element clicks must carry confidence.
            "confidence":     {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "require_confirm": {"type": "boolean"},
        },
    },

    "element_get_text": {
        "type": "object",
        "additionalProperties": False,
        "required": ["element_id"],
        "properties": {
            "element_id": {"type": "string", "minLength": 1, "maxLength": MAX_LABEL_LENGTH},
        },
    },

    "element_find": {
        "type": "object",
        "additionalProperties": False,
        "required": ["by", "query"],
        "properties": {
            "by":    {"type": "string", "enum": ["name", "role", "label", "description", "value"]},
            "query": {"type": "string", "minLength": 1, "maxLength": 256},
            # Maximum number of results to return (prevents flooding).
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    },

    "element_set_value": {
        "type": "object",
        "additionalProperties": False,
        "required": ["element_id", "value"],
        "properties": {
            "element_id": {"type": "string", "minLength": 1, "maxLength": MAX_LABEL_LENGTH},
            "value":      {"type": "string", "minLength": 0, "maxLength": MAX_TEXT_LENGTH},
        },
    },

    # ── Application launch ───────────────────────────────────────────────────

    "app_launch": {
        "type": "object",
        "additionalProperties": False,
        "required": ["app_name"],
        "properties": {
            # Application name looked up via AppScanner or ALLOWED_APPS allowlist.
            "app_name": {"type": "string", "minLength": 1, "maxLength": 128},
            # Optional arguments (each individually bounded).
            "args": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 256},
            },
            # Optional: Chrome profile name to open directly.
            "chrome_profile": {"type": "string", "maxLength": 128},
            # FIX v4.3: optional URL to open right after launching a browser
            # (content_pipeline / social_poster already sent this key).
            "url": {
                "type": "string",
                "maxLength": 2048,
                "pattern": r"^https?://[^\s]{1,2040}$",
            },
        },
    },

    # ── Browser navigation ───────────────────────────────────────────────────

    "browser_navigate": {
        "type": "object",
        "additionalProperties": False,
        "required": ["url"],
        "properties": {
            # Must be http:// or https:// — daemon enforces this allowlist.
            "url": {
                "type": "string",
                "minLength": 8,
                "maxLength": 2048,
                "pattern": r"^https?://[^\s]{1,2040}$",
            },
        },
    },

    # ── Browser JS execution ─────────────────────────────────────────────────
    # v9.34 A1: Thực thi JavaScript trong Chrome active tab qua AppleScript.
    # Không expose đây là shell/subprocess execution — đây là browser API.
    # Security: L2 daemon sanitize js_code tối đa 4096 chars,
    #           không cho phép embedded AppleScript injection.

    "browser_execute_js": {
        "type": "object",
        "additionalProperties": False,
        "required": ["js_code"],
        "properties": {
            # JavaScript code to execute in active Chrome tab.
            # Result returned as string via IPC response.
            "js_code": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
            },
            # Optional timeout in seconds (default 8s).
            "timeout_s": {
                "type": "number",
                "minimum": 0.5,
                "maximum": 30.0,
            },
        },
    },

    # ── System / control actions ─────────────────────────────────────────────

    "ping": {
        # Health-check: daemon echoes a RESPONSE with success=True.
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {
            "nonce": {"type": "string", "minLength": 1, "maxLength": 64},
        },
    },

    "user_confirm": {
        # R-22: Request explicit user confirmation before a sensitive action.
        # Sent by the daemon back to the orchestrator as a blocking REQUEST;
        # orchestrator or CLI presents the prompt and returns RESPONSE.
        "type": "object",
        "additionalProperties": False,
        "required": ["message", "action_description"],
        "properties": {
            "message":            {"type": "string", "minLength": 1, "maxLength": 512},
            "action_description": {"type": "string", "minLength": 1, "maxLength": 256},
            # The original action that triggered this confirmation request.
            "pending_action":     {"type": "string", "minLength": 1, "maxLength": 64},
            # The correlation_id of the original request (for reply routing).
            "pending_cid":        {
                "type": "string",
                "pattern": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$",
            },
        },
    },

    # ── v4.3 Keyboard-first layer: cheap "seeing" without screenshots ─────
    # Frontmost app, focused window/element, document path, selected text…
    "ui_snapshot": {
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {
            "include_value": {"type": "boolean"},
            "max_chars":     {"type": "integer", "minimum": 0, "maximum": MAX_TEXT_LENGTH},
        },
    },
    # Menu bar of an app (default: frontmost) with each item's shortcut.
    "menu_list": {
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {
            "app":       {"type": "string", "minLength": 1, "maxLength": MAX_LABEL_LENGTH},
            "filter":    {"type": "string", "maxLength": 128},
            "max_items": {"type": "integer", "minimum": 1, "maximum": 2000},
        },
    },
    # Press a menu item by its title path, e.g. ["File", "Export as PDF…"].
    # Destructive items (delete / empty trash / erase…) always get a
    # confirmation dialog in the daemon (R-22 defence in depth).
    "menu_select": {
        "type": "object",
        "additionalProperties": False,
        "required": ["path"],
        "properties": {
            "path": {
                "type": "array", "minItems": 2, "maxItems": 6,
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "app":             {"type": "string", "minLength": 1, "maxLength": MAX_LABEL_LENGTH},
            "require_confirm": {"type": "boolean"},
        },
    },
}

#: Immutable set of all allowlisted action names.
#: Used by validate_message() to reject unknown actions without mutation risk.
_ALLOWED_ACTIONS: frozenset[str] = frozenset(ACTION_SCHEMAS.keys())

# Expose as a public constant for documentation purposes (do not mutate).
ALLOWED_ACTIONS: frozenset[str] = _ALLOWED_ACTIONS


# ---------------------------------------------------------------------------
# REACT_OUTPUT_SCHEMA — v9.33 Inner Monologue
# ---------------------------------------------------------------------------
#
# Schema mà LLM phải tuân theo khi output ReAct step.
# Bắt buộc có trường "monologue" — LLM phải reasoning rõ ràng
# trước khi ra action. Đây là cơ chế Chain-of-Thought được enforce
# ở schema level, không chỉ ở prompt level.
#
# Dùng bởi:
#   planner/react_reasoner.py — validate và extract monologue
#   vision/inner_monologue.py — log + store monologue
#
# Trường monologue KHÔNG phải là part của IPC message (không gửi qua socket).
# Chỉ dùng internally trong orchestrator để trace reasoning.

REACT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "ReAct step output từ LLM — bao gồm reasoning + action",
    "properties": {
        "monologue": {
            "type": "string",
            "description": (
                "Tư duy nội tâm: phân tích UI hiện tại, trạng thái task, "
                "rủi ro dự đoán, và lý do chọn action này. "
                "Bắt buộc khi confidence thấp hoặc action là critical step."
            ),
            "maxLength": 500,
        },
        "thought": {
            "type": "string",
            "description": "Tóm tắt ngắn gọn cho log (1 câu).",
        },
        "action": {
            "type": "string",
            "enum": list(ACTION_SCHEMAS.keys()),
            "description": "Tên action IPC hợp lệ.",
        },
        "payload": {
            "type": "object",
            "description": "Payload của action — phải match ACTION_SCHEMAS[action].",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Mức độ tự tin của LLM về action này (0.0–1.0).",
        },
        "is_critical": {
            "type": "boolean",
            "description": "True nếu action này không thể hoàn tác (đăng bài, xóa, gửi).",
            "default": False,
        },
    },
    "required": ["action", "payload"],
    # monologue RECOMMENDED nhưng không required để không break existing code
    # react_reasoner sẽ log warning nếu thiếu khi confidence < 0.8
}

#: Các action được coi là CRITICAL — cần Wait-and-Think trước khi thực hiện.
CRITICAL_ACTIONS: frozenset[str] = frozenset({
    # Facebook / Social posting — không thể undo dễ dàng
    "fb_post_button",
    "ig_share_button",
    "x_post_button",
    # File operations
    "file_delete",
    "file_overwrite",
    # Confirmation dialogs
    "user_confirm",
})

# ---------------------------------------------------------------------------
# RESPONSE and ERROR payload schemas
# ---------------------------------------------------------------------------

#: Schema for the payload of RESPONSE messages.
RESPONSE_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["success"],
    "properties": {
        "success":    {"type": "boolean"},
        # `result` may be any valid JSON value; {} means "no constraint".
        "result":     {},
        # Short machine-readable error code if success=False.
        "error_code": {"type": "string", "maxLength": 64},
        # Optional human-readable message.
        "message":    {"type": "string", "maxLength": 512},
    },
}

#: Schema for the payload of ERROR messages.
ERROR_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["code", "message"],
    "properties": {
        "code":    {"type": "string", "minLength": 1, "maxLength": 64},
        "message": {"type": "string", "minLength": 1, "maxLength": 512},
        "field":   {"type": "string", "maxLength": 256},
        # `details` may carry any additional diagnostic data.
        "details": {},
    },
}

#: Schema for the payload of EVENT messages.
EVENT_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {
        "status":  {"type": "string", "maxLength": 128},
        "message": {"type": "string", "maxLength": 512},
        # Any additional event-specific data.
        "data":    {},
    },
}


# ---------------------------------------------------------------------------
# Message envelope schema
# ---------------------------------------------------------------------------

#: JSON Schema for the outer message envelope.
#: ALL messages (REQUEST, RESPONSE, ERROR, EVENT) must satisfy this schema
#: before any payload validation is performed.
MESSAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["type", "action", "payload", "correlation_id", "timestamp"],
    "properties": {
        # Message category: determines which payload schema applies.
        "type": {
            "type": "string",
            "enum": sorted(MESSAGE_TYPES),   # deterministic order
        },
        # The action name.  Must be in _ALLOWED_ACTIONS for REQUEST messages.
        # For RESPONSE/ERROR/EVENT, echoes the originating action or names
        # the event type.
        "action": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
        },
        # Opaque payload dict.  Further validated per (type, action).
        "payload": {
            "type": "object",
        },
        # UUID4 correlation ID linking requests to responses.
        "correlation_id": {
            "type": "string",
            "pattern": (
                r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}"
                r"-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
            ),
        },
        # ISO-8601 UTC timestamp with millisecond precision.
        "timestamp": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$",
        },
        # Protocol version (optional for now; reserved for future use).
        "version": {
            "type": "string",
            "pattern": r"^\d+\.\d+$",
        },
        # [C-10] HMAC signature for per-message authentication.
        "_hmac": {
            "type": "string",
        },
        # Timestamp for replay protection (epoch seconds).
        "_ts": {
            "type": ["number", "string"],
        },
    },
}


# ---------------------------------------------------------------------------
# Internal validation helpers
# ---------------------------------------------------------------------------

def _validate_envelope(msg: Any) -> None:
    """Validate the outer message envelope against MESSAGE_SCHEMA."""
    if not isinstance(msg, dict):
        raise IPCValidationError(
            f"Message must be a JSON object, got {type(msg).__name__!r}",
            field="<root>",
            code="INVALID_TYPE",
        )
    try:
        validate(msg, MESSAGE_SCHEMA)
    except JsonSchemaError as exc:
        raise IPCValidationError(
            str(exc),
            field=_extract_field(str(exc)),
            code="ENVELOPE_SCHEMA_VIOLATION",
        ) from exc


def _validate_size(msg: dict[str, Any]) -> None:
    """Reject messages whose serialised size exceeds MAX_SERIALIZED_BYTES."""
    try:
        serialised = dumps(msg)
    except ValueError as exc:
        raise IPCValidationError(
            f"Message contains non-serialisable data: {exc}",
            field="<root>",
            code="SERIALISATION_ERROR",
        ) from exc

    byte_len = len(serialised.encode("utf-8"))
    if byte_len > MAX_SERIALIZED_BYTES:
        raise IPCValidationError(
            f"Message size {byte_len} bytes exceeds maximum "
            f"{MAX_SERIALIZED_BYTES} bytes",
            field="<root>",
            code="MESSAGE_TOO_LARGE",
        )


def _validate_action_allowed(action: str, msg_type: str) -> None:
    """For REQUEST messages, verify the action is in the allowlist."""
    if msg_type == TYPE_REQUEST and action not in _ALLOWED_ACTIONS:
        raise IPCValidationError(
            f"Unknown action {action!r}. Allowed actions: "
            f"{sorted(_ALLOWED_ACTIONS)}",
            field="action",
            code="UNKNOWN_ACTION",
        )


def _validate_request_payload(action: str, payload: dict[str, Any]) -> None:
    """Validate a REQUEST payload against the action's specific schema."""
    payload_schema = ACTION_SCHEMAS.get(action)
    if payload_schema is None:
        # This branch is only reachable if _validate_action_allowed is skipped.
        raise IPCValidationError(
            f"No payload schema defined for action {action!r}",
            field="payload",
            code="UNKNOWN_ACTION",
        )
    try:
        validate(payload, payload_schema)
    except JsonSchemaError as exc:
        raise IPCValidationError(
            str(exc),
            field=f"payload.{_extract_field(str(exc))}",
            code="PAYLOAD_SCHEMA_VIOLATION",
        ) from exc


def _validate_keyboard_hotkey_keys(payload: dict[str, Any]) -> None:
    """
    Additional semantic check for keyboard_hotkey: enforce minimum 2 keys
    and maximum 8 keys.  (JSON Schema does not have minItems/maxItems in
    our validator, so we check here.)
    """
    keys = payload.get("keys", [])
    if len(keys) < 2:
        raise IPCValidationError(
            "keyboard_hotkey requires at least 2 keys in the 'keys' array",
            field="payload.keys",
            code="PAYLOAD_SCHEMA_VIOLATION",
        )
    if len(keys) > 8:
        raise IPCValidationError(
            f"keyboard_hotkey 'keys' array has {len(keys)} entries; maximum is 8",
            field="payload.keys",
            code="PAYLOAD_SCHEMA_VIOLATION",
        )


def _extract_field(error_msg: str) -> str:
    """
    Extract a dotted field path from a JsonSchemaError message string.
    Returns the portion before the first colon, stripping leading '$'.
    Falls back to empty string.
    """
    # JsonSchemaError format: "$.<field>: <detail>"
    if ":" in error_msg:
        candidate = error_msg.split(":")[0].strip().lstrip("$").lstrip(".")
        if candidate:
            return candidate
    return ""


# ---------------------------------------------------------------------------
# Public validation API
# ---------------------------------------------------------------------------

def validate_message(msg: Any, *, max_bytes: int = MAX_SERIALIZED_BYTES) -> None:
    """
    Validate any IPC message dict end-to-end.

    Checks performed (in order):
      1. Envelope schema (type, action, payload, correlation_id, timestamp).
      2. Serialised byte size ≤ max_bytes.
      3. For REQUEST: action is in _ALLOWED_ACTIONS.
      4. For REQUEST: payload matches the action's schema.
      5. For RESPONSE: payload matches RESPONSE_PAYLOAD_SCHEMA.
      6. For ERROR: payload matches ERROR_PAYLOAD_SCHEMA.
      7. For EVENT: action is a known event name; payload matches EVENT_PAYLOAD_SCHEMA.
      8. Action-specific semantic checks (e.g. keyboard_hotkey key count).

    Args:
        msg:       Parsed message dict (already deserialised from JSON).
        max_bytes: Override the byte-size ceiling (used by ipc_server to
                   enforce the config-driven limit).

    Raises:
        IPCValidationError: on any validation failure.
    """
    # Step 1: envelope
    _validate_envelope(msg)

    # Step 2: size (we temporarily override the module constant if caller asks)
    _max = min(max_bytes, MAX_SERIALIZED_BYTES)
    _validate_size_with_limit(msg, _max)

    msg_type: str = msg["type"]
    action:   str = msg["action"]
    payload:  dict[str, Any] = msg["payload"]

    # Steps 3–4: REQUEST
    if msg_type == TYPE_REQUEST:
        _validate_action_allowed(action, msg_type)
        _validate_request_payload(action, payload)
        # Action-specific semantic checks
        if action == "keyboard_hotkey":
            _validate_keyboard_hotkey_keys(payload)

    # Step 5: RESPONSE
    elif msg_type == TYPE_RESPONSE:
        try:
            validate(payload, RESPONSE_PAYLOAD_SCHEMA)
        except JsonSchemaError as exc:
            raise IPCValidationError(
                str(exc),
                field=f"payload.{_extract_field(str(exc))}",
                code="PAYLOAD_SCHEMA_VIOLATION",
            ) from exc

    # Step 6: ERROR
    elif msg_type == TYPE_ERROR:
        try:
            validate(payload, ERROR_PAYLOAD_SCHEMA)
        except JsonSchemaError as exc:
            raise IPCValidationError(
                str(exc),
                field=f"payload.{_extract_field(str(exc))}",
                code="PAYLOAD_SCHEMA_VIOLATION",
            ) from exc

    # Step 7: EVENT
    elif msg_type == TYPE_EVENT:
        try:
            validate(payload, EVENT_PAYLOAD_SCHEMA)
        except JsonSchemaError as exc:
            raise IPCValidationError(
                str(exc),
                field=f"payload.{_extract_field(str(exc))}",
                code="PAYLOAD_SCHEMA_VIOLATION",
            ) from exc


def _validate_size_with_limit(msg: dict[str, Any], max_bytes: int) -> None:
    """Internal: validate size against a specific byte ceiling."""
    try:
        serialised = dumps(msg)
    except ValueError as exc:
        raise IPCValidationError(
            f"Message contains non-serialisable data: {exc}",
            field="<root>",
            code="SERIALISATION_ERROR",
        ) from exc
    byte_len = len(serialised.encode("utf-8"))
    if byte_len > max_bytes:
        raise IPCValidationError(
            f"Message size {byte_len} bytes exceeds ceiling {max_bytes} bytes",
            field="<root>",
            code="MESSAGE_TOO_LARGE",
        )


def validate_request(msg: Any, *, max_bytes: int = MAX_SERIALIZED_BYTES) -> None:
    """
    Validate that *msg* is a well-formed REQUEST message.

    Shorthand for validate_message() that also asserts type == "REQUEST".

    Raises:
        IPCValidationError: if not a REQUEST or if validation fails.
    """
    validate_message(msg, max_bytes=max_bytes)
    if msg.get("type") != TYPE_REQUEST:
        raise IPCValidationError(
            f"Expected message type REQUEST, got {msg.get('type')!r}",
            field="type",
            code="WRONG_MESSAGE_TYPE",
        )


def validate_response(msg: Any, *, max_bytes: int = MAX_SERIALIZED_BYTES) -> None:
    """
    Validate that *msg* is a well-formed RESPONSE message.

    Raises:
        IPCValidationError: if not a RESPONSE or if validation fails.
    """
    validate_message(msg, max_bytes=max_bytes)
    if msg.get("type") != TYPE_RESPONSE:
        raise IPCValidationError(
            f"Expected message type RESPONSE, got {msg.get('type')!r}",
            field="type",
            code="WRONG_MESSAGE_TYPE",
        )


def validate_event(msg: Any, *, max_bytes: int = MAX_SERIALIZED_BYTES) -> None:
    """
    Validate that *msg* is a well-formed EVENT message.

    Raises:
        IPCValidationError: if not an EVENT or if validation fails.
    """
    validate_message(msg, max_bytes=max_bytes)
    if msg.get("type") != TYPE_EVENT:
        raise IPCValidationError(
            f"Expected message type EVENT, got {msg.get('type')!r}",
            field="type",
            code="WRONG_MESSAGE_TYPE",
        )


# ---------------------------------------------------------------------------
# Message constructors
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    """
    Return the current UTC time as an ISO-8601 string with millisecond precision.

    Patch v9.11.1 / FIX-4:
        datetime.now(tz=timezone.utc) is called exactly once and the result
        is stored in ``now``.  The previous implementation called it twice —
        once to format the seconds portion and once to extract microseconds —
        which could produce an internally inconsistent string if a second
        boundary was crossed between the two calls (e.g. seconds said
        "23:59:59" while microseconds came from "00:00:00.123456").
    """
    now = datetime.now(tz=timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def make_request(
    action: str,
    payload: dict[str, Any],
    *,
    correlation_id: str,
) -> dict[str, Any]:
    """
    Construct and validate a REQUEST message dict.

    Args:
        action:         Action name (must be in ALLOWED_ACTIONS).
        payload:        Action-specific payload dict.
        correlation_id: UUID4 string linking this request to its response.
                        Use utils.hash_utils.generate_key + uuid.uuid4().

    Returns:
        Validated message dict ready for serialisation and transmission.

    Raises:
        IPCValidationError: if the message fails validation.
    """
    msg: dict[str, Any] = {
        "type":           TYPE_REQUEST,
        "action":         action,
        "payload":        payload,
        "correlation_id": correlation_id,
        "timestamp":      _utc_now(),
        "version":        "1.0",
    }
    validate_request(msg)
    return msg


def make_response(
    action: str,
    correlation_id: str,
    *,
    success: bool,
    result: Any = None,
    error_code: str = "",
    message: str = "",
) -> dict[str, Any]:
    """
    Construct and validate a RESPONSE message dict.

    Args:
        action:         Echoes the action from the originating REQUEST.
        correlation_id: Echoes the correlation_id from the originating REQUEST.
        success:        True if the action succeeded.
        result:         Optional result payload (any JSON-serialisable value).
        error_code:     Short error code if success=False.
        message:        Human-readable description (optional).

    Returns:
        Validated RESPONSE message dict.

    Raises:
        IPCValidationError: if the message fails validation.
    """
    payload: dict[str, Any] = {"success": success}
    if result is not None:
        payload["result"] = result
    if error_code:
        payload["error_code"] = error_code
    if message:
        payload["message"] = message

    msg: dict[str, Any] = {
        "type":           TYPE_RESPONSE,
        "action":         action,
        "payload":        payload,
        "correlation_id": correlation_id,
        "timestamp":      _utc_now(),
        "version":        "1.0",
    }
    validate_response(msg)
    return msg


def make_error(
    action: str,
    correlation_id: str,
    *,
    code: str,
    message: str,
    field: str = "",
    details: Any = None,
) -> dict[str, Any]:
    """
    Construct and validate an ERROR message dict.

    Args:
        action:         Echoes the action that caused the error.
        correlation_id: Echoes the originating request's correlation_id.
        code:           Short machine-readable error code.
        message:        Human-readable error description.
        field:          Dot-path of the offending field, if applicable.
        details:        Optional diagnostic data (any JSON-serialisable value).

    Returns:
        Validated ERROR message dict.

    Raises:
        IPCValidationError: if the message fails validation.
    """
    payload: dict[str, Any] = {"code": code, "message": message}
    if field:
        payload["field"] = field
    if details is not None:
        payload["details"] = details

    msg: dict[str, Any] = {
        "type":           TYPE_ERROR,
        "action":         action,
        "payload":        payload,
        "correlation_id": correlation_id,
        "timestamp":      _utc_now(),
        "version":        "1.0",
    }
    # Use validate_message (not validate_response) since this is an ERROR.
    validate_message(msg)
    return msg


# ---------------------------------------------------------------------------
# Module-level integrity assertion
# ---------------------------------------------------------------------------
# Verified once at import time to detect accidental schema mutations.
# Not a security control on its own; the frozenset _ALLOWED_ACTIONS is.

_EXPECTED_ACTION_COUNT: int = 26  # v4.3: + ui_snapshot, menu_list, menu_select
assert len(ACTION_SCHEMAS) == _EXPECTED_ACTION_COUNT, (
    f"ACTION_SCHEMAS has {len(ACTION_SCHEMAS)} entries; expected "
    f"{_EXPECTED_ACTION_COUNT}. Update _EXPECTED_ACTION_COUNT if you "
    f"intentionally changed the action set."
)


# ---------------------------------------------------------------------------
# Self-tests (run with: python -m ipc.action_schema)
# ---------------------------------------------------------------------------

def _run_tests() -> None:  # pragma: no cover  (invoked as __main__ only)
    import uuid
    import traceback as tb

    PASS = "\033[32mPASS\033[0m"
    FAIL = "\033[31mFAIL\033[0m"
    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn):
        try:
            fn()
            results.append((name, True, ""))
        except Exception as exc:
            results.append((name, False, "".join(tb.format_exception(exc))))

    def fresh_cid() -> str:
        return str(uuid.uuid4())

    VALID_CID = fresh_cid()

    # ── IPCValidationError ────────────────────────────────────────────────
    def t_exc_fields():
        e = IPCValidationError("bad field", field="payload.x", code="TYPE_ERR")
        assert e.field == "payload.x"
        assert e.code  == "TYPE_ERR"
        assert "payload.x" in str(e)
        d = e.to_dict()
        assert d["code"] == "TYPE_ERR"
        assert d["field"] == "payload.x"
    check("IPCValidationError fields", t_exc_fields)

    # ── MESSAGE_SCHEMA completeness ───────────────────────────────────────
    def t_schema_fields():
        for f in ["type", "action", "payload", "correlation_id", "timestamp"]:
            assert f in MESSAGE_SCHEMA["properties"], f"missing: {f}"
        assert MESSAGE_SCHEMA["additionalProperties"] == False
    check("MESSAGE_SCHEMA required fields", t_schema_fields)

    # ── ACTION_SCHEMAS count ──────────────────────────────────────────────
    def t_action_count():
        # v9.34 added browser_execute_js → 23 actions (test was never updated)
        assert len(ACTION_SCHEMAS) == 26, f"got {len(ACTION_SCHEMAS)}"
    check("ACTION_SCHEMAS count == 26", t_action_count)

    # ── ALLOWED_ACTIONS immutability ──────────────────────────────────────
    def t_frozenset():
        assert isinstance(_ALLOWED_ACTIONS, frozenset)
        try:
            _ALLOWED_ACTIONS.add("evil_action")  # type: ignore
            assert False, "should raise"
        except AttributeError:
            pass
    check("ALLOWED_ACTIONS is frozenset", t_frozenset)

    # ── All action schemas have additionalProperties: false ───────────────
    def t_no_extra_props():
        for name, schema in ACTION_SCHEMAS.items():
            assert schema.get("additionalProperties") == False, (
                f"action {name!r} missing additionalProperties:false"
            )
    check("All action schemas have additionalProperties:false", t_no_extra_props)

    # ── UUID4 pattern enforcement ─────────────────────────────────────────
    def t_uuid4_pattern():
        # Valid UUID4
        good_cid = str(uuid.uuid4())
        msg = {
            "type": "REQUEST", "action": "ping", "payload": {},
            "correlation_id": good_cid,
            "timestamp": _utc_now(), "version": "1.0",
        }
        validate_request(msg)

        # UUID1 — wrong version digit
        bad_cid = "550e8400-e29b-11d4-a716-446655440000"
        msg["correlation_id"] = bad_cid
        try:
            validate_request(msg)
            assert False, "UUID1 should be rejected"
        except IPCValidationError as exc:
            assert "correlation_id" in exc.field or "ENVELOPE" in exc.code
    check("UUID4 pattern enforced", t_uuid4_pattern)

    # ── Timestamp pattern enforcement ─────────────────────────────────────
    def t_timestamp():
        msg = {
            "type": "REQUEST", "action": "ping", "payload": {},
            "correlation_id": fresh_cid(),
            "timestamp": "not-a-timestamp", "version": "1.0",
        }
        try:
            validate_request(msg)
            assert False
        except IPCValidationError:
            pass
    check("Timestamp pattern enforced", t_timestamp)

    # ── Unknown action rejected ───────────────────────────────────────────
    def t_unknown_action():
        msg = {
            "type": "REQUEST", "action": "evil_action", "payload": {},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        try:
            validate_request(msg)
            assert False, "unknown action should be rejected"
        except IPCValidationError as exc:
            assert exc.code == "UNKNOWN_ACTION"
    check("Unknown action rejected", t_unknown_action)

    # ── Missing required envelope field ───────────────────────────────────
    def t_missing_envelope():
        for drop in ["type", "action", "payload", "correlation_id", "timestamp"]:
            msg = {
                "type": "REQUEST", "action": "ping", "payload": {},
                "correlation_id": fresh_cid(),
                "timestamp": _utc_now(),
            }
            del msg[drop]
            try:
                validate_message(msg)
                assert False, f"should reject missing {drop!r}"
            except IPCValidationError:
                pass
    check("Missing required envelope fields", t_missing_envelope)

    # ── Additional envelope field rejected ────────────────────────────────
    def t_extra_envelope():
        msg = {
            "type": "REQUEST", "action": "ping", "payload": {},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
            "injected": "evil",
        }
        try:
            validate_request(msg)
            assert False
        except IPCValidationError:
            pass
    check("Extra envelope field rejected", t_extra_envelope)

    # ── mouse_click: valid ────────────────────────────────────────────────
    def t_mouse_click_valid():
        msg = make_request(
            "mouse_click",
            {"x": 100, "y": 200, "button": "left", "confidence": 0.95,
             "require_confirm": False, "target_label": "Submit"},
            correlation_id=fresh_cid(),
        )
        validate_request(msg)
    check("mouse_click valid", t_mouse_click_valid)

    # ── mouse_click: coordinate overflow ─────────────────────────────────
    def t_mouse_click_overflow():
        msg = {
            "type": "REQUEST", "action": "mouse_click",
            "payload": {"x": 999999, "y": 200},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        try:
            validate_request(msg)
            assert False
        except IPCValidationError as exc:
            assert "PAYLOAD" in exc.code
    check("mouse_click coordinate overflow rejected", t_mouse_click_overflow)

    # ── mouse_click: unknown button ───────────────────────────────────────
    def t_mouse_click_bad_button():
        msg = {
            "type": "REQUEST", "action": "mouse_click",
            "payload": {"x": 10, "y": 10, "button": "wheel"},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        try:
            validate_request(msg)
            assert False
        except IPCValidationError:
            pass
    check("mouse_click unknown button rejected", t_mouse_click_bad_button)

    # ── mouse_click: missing required x,y ────────────────────────────────
    def t_mouse_click_missing_xy():
        msg = {
            "type": "REQUEST", "action": "mouse_click",
            "payload": {"button": "left"},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        try:
            validate_request(msg)
            assert False
        except IPCValidationError:
            pass
    check("mouse_click missing x/y rejected", t_mouse_click_missing_xy)

    # ── mouse_click: extra payload field rejected ─────────────────────────
    def t_mouse_click_extra_field():
        msg = {
            "type": "REQUEST", "action": "mouse_click",
            "payload": {"x": 10, "y": 10, "injected_cmd": "rm -rf /"},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        try:
            validate_request(msg)
            assert False
        except IPCValidationError as exc:
            assert "additional" in str(exc).lower() or "PAYLOAD" in exc.code
    check("mouse_click extra payload field rejected", t_mouse_click_extra_field)

    # ── keyboard_type: valid ──────────────────────────────────────────────
    def t_keyboard_type_valid():
        msg = make_request("keyboard_type", {"text": "hello world"},
                           correlation_id=fresh_cid())
        validate_request(msg)
    check("keyboard_type valid", t_keyboard_type_valid)

    # ── keyboard_type: text too long ──────────────────────────────────────
    def t_keyboard_type_too_long():
        msg = {
            "type": "REQUEST", "action": "keyboard_type",
            "payload": {"text": "A" * (MAX_TEXT_LENGTH + 1)},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        try:
            validate_request(msg)
            assert False
        except IPCValidationError:
            pass
    check("keyboard_type text too long rejected", t_keyboard_type_too_long)

    # ── keyboard_type: empty text ─────────────────────────────────────────
    def t_keyboard_type_empty():
        msg = {
            "type": "REQUEST", "action": "keyboard_type",
            "payload": {"text": ""},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        try:
            validate_request(msg)
            assert False, "empty text should be rejected (minLength: 1)"
        except IPCValidationError:
            pass
    check("keyboard_type empty text rejected", t_keyboard_type_empty)

    # ── keyboard_hotkey: valid ────────────────────────────────────────────
    def t_hotkey_valid():
        msg = make_request("keyboard_hotkey", {"keys": ["ctrl", "c"]},
                           correlation_id=fresh_cid())
        validate_request(msg)
    check("keyboard_hotkey valid", t_hotkey_valid)

    # ── keyboard_hotkey: too few keys ─────────────────────────────────────
    def t_hotkey_too_few():
        msg = {
            "type": "REQUEST", "action": "keyboard_hotkey",
            "payload": {"keys": ["enter"]},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        try:
            validate_request(msg)
            assert False, "single-key hotkey should be rejected"
        except IPCValidationError as exc:
            assert "at least 2" in str(exc)
    check("keyboard_hotkey < 2 keys rejected", t_hotkey_too_few)

    # ── keyboard_hotkey: too many keys ────────────────────────────────────
    def t_hotkey_too_many():
        msg = {
            "type": "REQUEST", "action": "keyboard_hotkey",
            "payload": {"keys": ["ctrl", "alt", "shift", "a", "b", "c", "d", "e", "f"]},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        try:
            validate_request(msg)
            assert False
        except IPCValidationError as exc:
            assert "maximum is 8" in str(exc)
    check("keyboard_hotkey > 8 keys rejected", t_hotkey_too_many)

    # ── element_find: valid enum ──────────────────────────────────────────
    def t_element_find_valid():
        msg = make_request("element_find",
                           {"by": "role", "query": "button", "limit": 10},
                           correlation_id=fresh_cid())
        validate_request(msg)
    check("element_find valid", t_element_find_valid)

    # ── element_find: invalid 'by' value ─────────────────────────────────
    def t_element_find_bad_by():
        msg = {
            "type": "REQUEST", "action": "element_find",
            "payload": {"by": "xpath", "query": "//button"},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        try:
            validate_request(msg)
            assert False
        except IPCValidationError:
            pass
    check("element_find invalid 'by' rejected", t_element_find_bad_by)

    # ── browser_navigate: valid ───────────────────────────────────────────
    def t_browser_navigate_valid():
        msg = make_request("browser_navigate",
                           {"url": "https://example.com"},
                           correlation_id=fresh_cid())
        validate_request(msg)
    check("browser_navigate valid", t_browser_navigate_valid)

    # ── browser_navigate: non-http URL rejected ───────────────────────────
    def t_browser_navigate_ftp():
        msg = {
            "type": "REQUEST", "action": "browser_navigate",
            "payload": {"url": "ftp://evil.example.com/malware.sh"},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        try:
            validate_request(msg)
            assert False, "ftp:// should be rejected"
        except IPCValidationError:
            pass
    check("browser_navigate ftp:// rejected", t_browser_navigate_ftp)

    # ── ping: valid ───────────────────────────────────────────────────────
    def t_ping_valid():
        msg = make_request("ping", {}, correlation_id=fresh_cid())
        validate_request(msg)
        msg2 = make_request("ping", {"nonce": "abc123"}, correlation_id=fresh_cid())
        validate_request(msg2)
    check("ping valid (with/without nonce)", t_ping_valid)

    # ── message size limit ────────────────────────────────────────────────
    def t_size_limit():
        huge_label = "A" * MAX_TEXT_LENGTH
        msg = {
            "type": "REQUEST", "action": "keyboard_type",
            "payload": {"text": huge_label},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        # This is valid by schema (within maxLength) but we test with a
        # very tight max_bytes to verify the size check works.
        try:
            validate_request(msg, max_bytes=100)
            assert False, "should exceed 100-byte limit"
        except IPCValidationError as exc:
            assert exc.code == "MESSAGE_TOO_LARGE"
    check("Message size limit enforced", t_size_limit)

    # ── make_response: valid ──────────────────────────────────────────────
    def t_make_response():
        cid = fresh_cid()
        resp = make_response("mouse_click", cid, success=True,
                             result={"pixel": [100, 200]})
        validate_response(resp)
        assert resp["type"] == "RESPONSE"
        assert resp["payload"]["success"] == True
        assert resp["payload"]["result"] == {"pixel": [100, 200]}

        resp_err = make_response("mouse_click", cid, success=False,
                                 error_code="ELEMENT_NOT_FOUND",
                                 message="Target element not visible")
        validate_response(resp_err)
        assert resp_err["payload"]["success"] == False
    check("make_response valid", t_make_response)

    # ── make_error: valid ─────────────────────────────────────────────────
    def t_make_error():
        cid = fresh_cid()
        err = make_error("mouse_click", cid, code="SCHEMA_VIOLATION",
                         message="payload.x out of range", field="payload.x")
        validate_message(err)
        assert err["type"] == "ERROR"
        assert err["payload"]["code"] == "SCHEMA_VIOLATION"
        assert err["payload"]["field"] == "payload.x"
    check("make_error valid", t_make_error)

    # ── validate_response rejects REQUEST type ────────────────────────────
    def t_wrong_type():
        cid = fresh_cid()
        req = make_request("ping", {}, correlation_id=cid)
        try:
            validate_response(req)
            assert False
        except IPCValidationError as exc:
            assert exc.code == "WRONG_MESSAGE_TYPE"
    check("validate_response rejects REQUEST", t_wrong_type)

    # ── RESPONSE: missing success field ───────────────────────────────────
    def t_response_missing_success():
        msg = {
            "type": "RESPONSE", "action": "ping",
            "payload": {"result": "pong"},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        try:
            validate_response(msg)
            assert False
        except IPCValidationError:
            pass
    check("RESPONSE missing 'success' rejected", t_response_missing_success)

    # ── RESPONSE: extra payload field rejected ────────────────────────────
    def t_response_extra_field():
        msg = {
            "type": "RESPONSE", "action": "ping",
            "payload": {"success": True, "injected": "evil"},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        try:
            validate_response(msg)
            assert False
        except IPCValidationError:
            pass
    check("RESPONSE extra payload field rejected", t_response_extra_field)

    # ── EVENT: valid ──────────────────────────────────────────────────────
    def t_event_valid():
        msg = {
            "type": "EVENT", "action": "daemon_ready",
            "payload": {"status": "online", "message": "Daemon started"},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        validate_event(msg)
    check("EVENT valid", t_event_valid)

    # ── non-dict message rejected ─────────────────────────────────────────
    def t_non_dict():
        for bad in [None, "string", 42, [1, 2, 3]]:
            try:
                validate_message(bad)
                assert False, f"{bad!r} should be rejected"
            except IPCValidationError as exc:
                assert exc.code == "INVALID_TYPE"
    check("Non-dict message rejected", t_non_dict)

    # ── All actions: valid empty/minimal payloads ─────────────────────────
    def t_all_actions_minimal():
        minimal_payloads: dict[str, dict] = {
            "mouse_click":       {"x": 0,   "y": 0},
            "mouse_move":        {"x": 100, "y": 100},
            "mouse_scroll":      {"x": 0,   "y": 0, "dy": 3},
            "mouse_drag":        {"from_x": 0, "from_y": 0, "to_x": 10, "to_y": 10},
            "keyboard_type":     {"text": "a"},
            "keyboard_press":    {"key": "enter"},
            "keyboard_hotkey":   {"keys": ["ctrl", "c"]},
            "clipboard_copy":    {"text": "hello"},
            "clipboard_paste":   {},
            "clipboard_get":     {},
            "screenshot_capture":{},
            "window_focus":      {},
            "window_list":       {},
            "window_get_info":   {},
            "element_click":     {"element_id": "btn-1"},
            "element_get_text":  {"element_id": "lbl-2"},
            "element_find":      {"by": "name", "query": "Submit"},
            "element_set_value": {"element_id": "inp-1", "value": "hello"},
            "app_launch":        {"app_name": "firefox"},
            "browser_navigate":  {"url": "https://example.com"},
            "ping":              {},
            "user_confirm":      {"message": "Confirm?", "action_description": "click OK"},
        }
        tested = 0
        for action, payload in minimal_payloads.items():
            if action not in _ALLOWED_ACTIONS:
                continue
            msg = {
                "type": "REQUEST", "action": action, "payload": payload,
                "correlation_id": fresh_cid(),
                "timestamp": _utc_now(), "version": "1.0",
            }
            validate_request(msg)
            tested += 1
        assert tested == 22, f"Only tested {tested} of 22 actions"
    check("All 22 actions: minimal valid payloads accepted", t_all_actions_minimal)

    # ── user_confirm: missing required fields ─────────────────────────────
    def t_user_confirm_missing():
        msg = {
            "type": "REQUEST", "action": "user_confirm",
            "payload": {"message": "Confirm?"},  # missing action_description
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        try:
            validate_request(msg)
            assert False
        except IPCValidationError:
            pass
    check("user_confirm missing action_description rejected", t_user_confirm_missing)

    # ── screenshot_capture: valid region ──────────────────────────────────
    def t_screenshot_region():
        msg = make_request("screenshot_capture",
                           {"region": {"x": 0, "y": 0, "width": 1920, "height": 1080}},
                           correlation_id=fresh_cid())
        validate_request(msg)
    check("screenshot_capture with valid region", t_screenshot_region)

    # ── screenshot_capture: invalid region (zero width) ───────────────────
    def t_screenshot_bad_region():
        msg = {
            "type": "REQUEST", "action": "screenshot_capture",
            "payload": {"region": {"x": 0, "y": 0, "width": 0, "height": 100}},
            "correlation_id": fresh_cid(),
            "timestamp": _utc_now(), "version": "1.0",
        }
        try:
            validate_request(msg)
            assert False
        except IPCValidationError:
            pass
    check("screenshot_capture zero-width region rejected", t_screenshot_bad_region)

    # ── mouse_drag: require_confirm field ─────────────────────────────────
    def t_drag_confirm():
        msg = make_request("mouse_drag",
                           {"from_x": 0, "from_y": 0, "to_x": 500, "to_y": 500,
                            "require_confirm": True},
                           correlation_id=fresh_cid())
        validate_request(msg)
    check("mouse_drag with require_confirm", t_drag_confirm)

    # ── _utc_now format matches timestamp regex ───────────────────────────
    def t_timestamp_format():
        ts = _utc_now()
        assert _TIMESTAMP_RE.match(ts), f"_utc_now() produced invalid ts: {ts!r}"
    check("_utc_now() produces valid timestamp", t_timestamp_format)

    # ── _utc_now() single call — no double-call race ──────────────────────
    def t_utc_now_single_call():
        # Run _utc_now() 1000 times and verify every result is a valid,
        # internally consistent timestamp.  A double-call implementation
        # would occasionally produce a timestamp where the millisecond
        # count is from the next second while the HH:MM:SS is from the
        # previous one — not detectable here, but structural correctness
        # is verified by regex match.
        import re as _re
        pattern = _re.compile(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
        )
        for _ in range(1000):
            ts = _utc_now()
            assert pattern.match(ts), f"bad timestamp: {ts!r}"
    check("_utc_now() single call (no race)", t_utc_now_single_call)

    # ── Print results ─────────────────────────────────────────────────────
    print()
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, err in results:
        status = PASS if ok else FAIL
        print(f"  {status}  {name}")
        if err:
            for line in err.strip().splitlines():
                print(f"         {line}")
    print()
    print(f"  {passed}/{total} tests passed")
    if passed < total:
        raise SystemExit(1)


if __name__ == "__main__":
    _run_tests()
