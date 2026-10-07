# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/error_templates.py — Phidipus v2.4 Priority 1 / A3
═══════════════════════════════════════════════════════════════════

User-friendly error messages with suggested actions.

Instead of raw "vision_click: không tìm thấy 'nút Đăng bài'",
user sees: explanation + WHY it failed + HOW to fix it.

Supports 4 languages (vi/en/zh/ko).
"""
from __future__ import annotations
from typing import Optional

_LANG = "vi"  # default, overridden from config

def set_language(lang: str) -> None:
    global _LANG
    if lang in ("vi", "en", "zh", "ko"):
        _LANG = lang

def get_language() -> str:
    return _LANG

# ══════════════════════════════════════════════════════════════
# Error templates — each has: message + suggested fix
# ══════════════════════════════════════════════════════════════

TEMPLATES: dict[str, dict[str, str]] = {

    "vision_not_found": {
        "vi": (
            "❌ Không tìm thấy \"{element}\" trên màn hình.\n"
            "💡 Gợi ý:\n"
            "  • Thêm node \"wait 3s\" trước bước này để trang load xong\n"
            "  • Kiểm tra mô tả element có chính xác không\n"
            "  • Thử mô tả khác: ví dụ \"blue Post button\" thay vì chỉ \"Post\""
        ),
        "en": (
            "❌ Cannot find \"{element}\" on screen.\n"
            "💡 Tips:\n"
            "  • Add a \"wait 3s\" node before this step\n"
            "  • Check if element description is accurate\n"
            "  • Try a more specific description"
        ),
        "zh": (
            "❌ 在屏幕上找不到 \"{element}\"。\n"
            "💡 建议:\n"
            "  • 在此步骤前添加\"wait 3s\"节点\n"
            "  • 检查元素描述是否准确\n"
            "  • 尝试更具体的描述"
        ),
        "ko": (
            "❌ 화면에서 \"{element}\"을(를) 찾을 수 없습니다.\n"
            "💡 팁:\n"
            "  • 이 단계 전에 \"wait 3s\" 노드를 추가하세요\n"
            "  • 요소 설명이 정확한지 확인하세요\n"
            "  • 더 구체적인 설명을 시도하세요"
        ),
    },

    "vision_low_confidence": {
        "vi": (
            "⚠️ Tìm thấy \"{element}\" nhưng độ tin cậy thấp ({confidence}%).\n"
            "💡 Gợi ý:\n"
            "  • Thêm domain + action_key để hệ thống nhớ vị trí\n"
            "  • Lần chạy sau sẽ nhanh và chính xác hơn nhờ cache\n"
            "  • Hoặc thêm js_selectors làm backup"
        ),
        "en": (
            "⚠️ Found \"{element}\" but confidence is low ({confidence}%).\n"
            "💡 Tips:\n"
            "  • Add domain + action_key so system remembers position\n"
            "  • Next run will be faster and more accurate via cache\n"
            "  • Or add js_selectors as backup"
        ),
        "zh": (
            "⚠️ 找到 \"{element}\" 但置信度低 ({confidence}%)。\n"
            "💡 建议:\n"
            "  • 添加domain + action_key让系统记住位置\n"
            "  • 下次运行会更快更准确\n"
            "  • 或添加js_selectors作为备选"
        ),
        "ko": (
            "⚠️ \"{element}\"을(를) 찾았지만 신뢰도가 낮습니다 ({confidence}%).\n"
            "💡 팁:\n"
            "  • domain + action_key를 추가하여 위치를 기억시키세요\n"
            "  • 다음 실행이 더 빠르고 정확해집니다\n"
            "  • 또는 js_selectors를 백업으로 추가하세요"
        ),
    },

    "ollama_offline": {
        "vi": (
            "❌ Ollama (AI engine) không phản hồi.\n"
            "💡 Cách khắc phục:\n"
            "  • Chạy lệnh: ollama serve\n"
            "  • Kiểm tra: curl http://127.0.0.1:11434/api/tags\n"
            "  • Đảm bảo đã pull model: ollama pull qwen3:8b"
        ),
        "en": (
            "❌ Ollama (AI engine) is not responding.\n"
            "💡 Fix:\n"
            "  • Run: ollama serve\n"
            "  • Check: curl http://127.0.0.1:11434/api/tags\n"
            "  • Ensure model pulled: ollama pull qwen3:8b"
        ),
        "zh": (
            "❌ Ollama (AI引擎) 无响应。\n"
            "💡 修复:\n"
            "  • 运行: ollama serve\n"
            "  • 检查: curl http://127.0.0.1:11434/api/tags\n"
            "  • 确保已拉取模型: ollama pull qwen3:8b"
        ),
        "ko": (
            "❌ Ollama (AI 엔진)가 응답하지 않습니다.\n"
            "💡 해결:\n"
            "  • 실행: ollama serve\n"
            "  • 확인: curl http://127.0.0.1:11434/api/tags\n"
            "  • 모델 다운로드 확인: ollama pull qwen3:8b"
        ),
    },

    "timeout": {
        "vi": (
            "⏱️ Bước này chạy quá lâu (>{seconds}s), đã bị timeout.\n"
            "💡 Gợi ý:\n"
            "  • Tăng timeout trong config node\n"
            "  • Chia thành 2 bước nhỏ hơn\n"
            "  • Kiểm tra kết nối mạng nếu node cần internet"
        ),
        "en": (
            "⏱️ This step took too long (>{seconds}s), timed out.\n"
            "💡 Tips:\n"
            "  • Increase timeout in node config\n"
            "  • Split into 2 smaller steps\n"
            "  • Check network if step requires internet"
        ),
        "zh": (
            "⏱️ 此步骤耗时过长 (>{seconds}s)，已超时。\n"
            "💡 建议:\n"
            "  • 增加节点配置中的超时时间\n"
            "  • 拆分为2个更小的步骤\n"
            "  • 如需网络，请检查连接"
        ),
        "ko": (
            "⏱️ 이 단계가 너무 오래 걸렸습니다 (>{seconds}s), 시간 초과.\n"
            "💡 팁:\n"
            "  • 노드 설정에서 시간 초과를 늘리세요\n"
            "  • 2개의 작은 단계로 나누세요\n"
            "  • 인터넷이 필요한 경우 네트워크를 확인하세요"
        ),
    },

    "config_missing_field": {
        "vi": (
            "⚠️ Node \"{node_type}\" thiếu field bắt buộc: \"{field}\".\n"
            "💡 Mở Workflow Teacher → click node → điền field \"{field}\" trong panel bên phải."
        ),
        "en": (
            "⚠️ Node \"{node_type}\" missing required field: \"{field}\".\n"
            "💡 Open Workflow Teacher → click node → fill \"{field}\" in right panel."
        ),
        "zh": (
            "⚠️ 节点 \"{node_type}\" 缺少必填字段: \"{field}\"。\n"
            "💡 打开工作流编辑器 → 点击节点 → 在右侧面板填写 \"{field}\"。"
        ),
        "ko": (
            "⚠️ 노드 \"{node_type}\"에 필수 필드 \"{field}\"가 없습니다.\n"
            "💡 워크플로우 편집기 → 노드 클릭 → 오른쪽 패널에서 \"{field}\" 입력."
        ),
    },

    "chrome_navigate_failed": {
        "vi": (
            "❌ Không mở được trang web: {url}\n"
            "💡 Gợi ý:\n"
            "  • Kiểm tra URL có đúng không (phải có https://)\n"
            "  • Kiểm tra Chrome đang mở và có kết nối mạng\n"
            "  • Thử mở thủ công trước, rồi chạy workflow"
        ),
        "en": (
            "❌ Cannot open webpage: {url}\n"
            "💡 Tips:\n"
            "  • Check URL format (must include https://)\n"
            "  • Ensure Chrome is open with network connection\n"
            "  • Try opening manually first"
        ),
        "zh": "❌ 无法打开网页: {url}\n💡 检查URL格式和网络连接。",
        "ko": "❌ 웹페이지를 열 수 없습니다: {url}\n💡 URL 형식과 네트워크 연결을 확인하세요.",
    },

    "ai_process_failed": {
        "vi": (
            "❌ AI không thể xử lý yêu cầu.\n"
            "💡 Gợi ý:\n"
            "  • Kiểm tra Ollama đang chạy: ollama serve\n"
            "  • Dữ liệu đầu vào có thể quá dài — thử giảm max_tokens\n"
            "  • Thử đổi model: auto → qwen3:8b hoặc gemini-2.5-flash"
        ),
        "en": (
            "❌ AI cannot process request.\n"
            "💡 Tips:\n"
            "  • Check Ollama is running: ollama serve\n"
            "  • Input may be too long — try reducing max_tokens\n"
            "  • Try different model: auto → qwen3:8b or gemini-2.5-flash"
        ),
        "zh": "❌ AI无法处理请求。\n💡 检查Ollama是否运行，或尝试其他模型。",
        "ko": "❌ AI가 요청을 처리할 수 없습니다.\n💡 Ollama가 실행 중인지 확인하거나 다른 모델을 시도하세요.",
    },

    "type_text_no_focus": {
        "vi": (
            "⚠️ Không có ô nhập nào đang focus trên trang.\n"
            "💡 Thêm node vision_click trước để click vào ô nhập trước khi gõ text."
        ),
        "en": "⚠️ No input field is focused.\n💡 Add a vision_click node before this to click the input field first.",
        "zh": "⚠️ 页面上没有输入框获得焦点。\n💡 在此之前添加vision_click节点来点击输入框。",
        "ko": "⚠️ 포커스된 입력 필드가 없습니다.\n💡 이 전에 vision_click 노드를 추가하여 입력 필드를 클릭하세요.",
    },

    "workflow_node_failed": {
        "vi": (
            "❌ Workflow dừng tại bước {step}/{total}: \"{node_type}\".\n"
            "💡 Mở Workflow Teacher → Test node này riêng lẻ để debug.\n"
            "💡 Xem screenshot lúc lỗi ở file đính kèm (nếu có)."
        ),
        "en": (
            "❌ Workflow stopped at step {step}/{total}: \"{node_type}\".\n"
            "💡 Open Workflow Teacher → Test this node individually to debug.\n"
            "💡 Check error screenshot attached (if available)."
        ),
        "zh": "❌ 工作流在步骤 {step}/{total} 停止: \"{node_type}\"。\n💡 打开编辑器 → 单独测试此节点。",
        "ko": "❌ 워크플로우가 {step}/{total} 단계에서 중지: \"{node_type}\".\n💡 편집기 → 이 노드를 개별 테스트하세요.",
    },
}


def friendly_error(
    template_key: str,
    lang: Optional[str] = None,
    **kwargs,
) -> str:
    """
    Get a user-friendly error message.

    Args:
        template_key: Key from TEMPLATES dict.
        lang: Language override (vi/en/zh/ko). None = use global.
        **kwargs: Format variables ({element}, {confidence}, etc.)

    Returns:
        Formatted error message string.
    """
    lang = lang or _LANG
    tmpl = TEMPLATES.get(template_key)
    if not tmpl:
        return f"❌ Error: {template_key} ({kwargs})"

    msg = tmpl.get(lang, tmpl.get("vi", ""))
    try:
        return msg.format(**kwargs)
    except (KeyError, IndexError):
        return msg
