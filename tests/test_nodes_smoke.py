# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
phidipus_e2e_smoke_test.py
──────────────────────────
Smoke test: kiểm tra từng node type không crash khi gọi execute_single_node.
Mỗi test chỉ cần "error" hoặc "success" trong kết quả — fail gracefully là OK.

Phát hiện ngay 3 P0 bugs trong audit (BUG-A, BUG-B, BUG-C) nếu chạy trước fix.

Chạy:
    python -m pytest tests/test_nodes_smoke.py -v
hoặc:
    python tests/test_nodes_smoke.py
"""

import asyncio
import sys
import os

# Thêm project root vào path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _make_executor():
    """Tạo WorkflowExecutor với mock dependencies."""
    from core.workflow_executor import WorkflowExecutor
    ex = WorkflowExecutor()
    return ex


def _valid_result(r: dict, node_name: str) -> bool:
    """Result hợp lệ nếu có 'success' key — kể cả success=False là chấp nhận được."""
    ok = isinstance(r, dict) and "success" in r
    if not ok:
        print(f"  ❌ {node_name}: invalid result shape: {r!r}")
    else:
        status = "✅" if r.get("success") else "⚠️ (graceful fail)"
        print(f"  {status} {node_name}: {r.get('error', r.get('result', ''))[:80]}")
    return ok


async def test_all_nodes_dont_crash():
    """
    Test mỗi node type với minimal config — không nên raise exception.
    Graceful failure (success=False + error message) là CHẤP NHẬN ĐƯỢC.
    Exception / AttributeError là KHÔNG chấp nhận.
    """
    ex = await _make_executor()

    # (node_type, config, prev_result)
    test_cases = [
        # P0 bugs — các node này crash trước khi fix
        ("generate_report", {"title": "Smoke Test", "ai_summary": True, "send_telegram": False}, ""),
        ("vision_read",     {"region_description": "test element", "data_type": "number"}, ""),
        ("ai_process",      {"action": "describe_image", "input_from": "screenshot"}, ""),

        # P1 bugs
        ("vision_wait_smart", {"condition": "Page fully loaded", "timeout": 1}, ""),
        ("terminal",          {"command": "echo smoke_test_ok"}, ""),

        # P2 bugs
        ("type_text",         {"text": "hello", "use_clipboard": True}, ""),
        ("hover_action",      {"description": "test button"}, ""),
        ("create_document",   {"format": "html", "title": "test"}, ""),

        # Stable nodes — kiểm tra không bị regression
        ("notify",            {"message": "smoke test"}, ""),
        ("screenshot",        {}, ""),
        ("wait",              {"seconds": 0.01}, ""),
        ("condition",         {"check": "variable_check", "variable": "x", "expected": "1"}, ""),
        ("vision_multi_detect", {"targets": [{"key": "btn", "description": "submit button"}]}, ""),
        ("app_launch",        {"app_name": "Terminal"}, ""),
        ("scroll",            {"direction": "down", "pixels": 100}, ""),
    ]

    failures = []
    print("\n" + "═" * 60)
    print("  PHIDIPUS NODE SMOKE TEST")
    print("═" * 60)

    for ntype, config, prev in test_cases:
        try:
            r = await ex.execute_single_node(ntype, config, prev)
            if not _valid_result(r, ntype):
                failures.append(f"{ntype}: invalid result shape")
        except Exception as exc:
            print(f"  💥 {ntype}: EXCEPTION → {type(exc).__name__}: {exc}")
            failures.append(f"{ntype}: {type(exc).__name__}: {exc}")

    print("═" * 60)
    if failures:
        print(f"\n❌ {len(failures)} node(s) CRASHED:")
        for f in failures:
            print(f"   • {f}")
        return False
    else:
        print(f"\n✅ All {len(test_cases)} nodes returned valid results (no crashes)")
        return True


async def test_schema_validation():
    """Test NODE_SCHEMAS validation catches known bad configs."""
    from core.workflow_executor import WorkflowExecutor
    ex = WorkflowExecutor()

    bad_cases = [
        # Missing required fields
        ("chrome",      {}, ["action"]),
        ("vision_click",{}, ["description"]),
        ("notify",      {}, ["message"]),
        ("condition",   {}, ["check"]),
        # Invalid values
        ("vision_read", {"region_description": "x", "data_type": "INVALID_TYPE"}, ["data_type"]),
        ("create_document", {"format": "pptx"}, ["format"]),   # pptx not in allowed values
        ("scroll", {"direction": "sideways"}, ["direction"]),
    ]

    print("\n" + "═" * 60)
    print("  SCHEMA VALIDATION TEST")
    print("═" * 60)

    ok_count = 0
    for ntype, config, expected_errors in bad_cases:
        errors = ex._validate_node_config(ntype, config)
        caught = any(
            any(e in err for e in expected_errors)
            for err in errors
        )
        if caught:
            print(f"  ✅ {ntype}: validation caught expected error(s) {expected_errors}")
            ok_count += 1
        else:
            print(f"  ⚠️ {ntype}: expected errors {expected_errors} NOT caught. Got: {errors}")

    print(f"\n{'✅' if ok_count == len(bad_cases) else '⚠️'} {ok_count}/{len(bad_cases)} validation checks passed")
    return ok_count == len(bad_cases)


if __name__ == "__main__":
    async def main():
        crash_ok = await test_all_nodes_dont_crash()
        schema_ok = await test_schema_validation()
        sys.exit(0 if (crash_ok and schema_ok) else 1)

    asyncio.run(main())
