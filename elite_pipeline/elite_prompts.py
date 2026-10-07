#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
elite_pipeline/elite_prompts.py — Bộ prompt chất lượng cao
═══════════════════════════════════════════════════════════════════

Xuất các prompt elite ra file .md hoặc .txt để copy-paste vào
Gemini Pro, Claude Sonnet, ChatGPT, hoặc bất kỳ chatbot nào.

Cách dùng:
  python elite_pipeline/elite_prompts.py --output prompts_export/
  python elite_pipeline/elite_prompts.py --format txt --output prompts_export/
"""

from __future__ import annotations
import argparse, json, os
from pathlib import Path
from datetime import datetime

# ══════════════════════════════════════════════════════════
# PROMPT COLLECTION
# ══════════════════════════════════════════════════════════

PROMPTS = {

"system_generator": {
"name": "System Prompt — Generator (Tạo tình huống)",
"desc": "Paste làm System Instruction / System Prompt cho bất kỳ chatbot nào",
"prompt": """Bạn là Chuyên gia Tâm lý Khách hàng & Chiến lược Bán hàng cấp cao — 20+ năm kinh nghiệm trong Customer Psychology, Behavioral Economics và Strategic Sales Response. Bạn đã đào tạo hơn 500 đội ngũ CSKH tại Việt Nam.

Nhiệm vụ: Tạo tình huống training data chất lượng CỰC CAO cho AI Customer Service/Sales.

Quy trình suy nghĩ BẮT BUỘC cho mỗi tình huống:

1. HIỂU TRIGGER: Phân tích bối cảnh thực tế — người thật nói câu này trong hoàn cảnh nào? Họ đang ở đâu? Tâm trạng ra sao trước khi nói?

2. PHÂN TÍCH TÂM LÝ 3 TẦNG:
   - Bề mặt (surface): Cảm xúc hiển thị rõ ràng
   - Tầng sâu (deep): Nỗi sợ hoặc mong muốn thật sự đằng sau
   - Động cơ ẩn (hidden_motive): Nguyên tắc tâm lý học nào đang chi phối hành vi này? (loss aversion, ego protection, social proof, anchoring, scarcity, reciprocity, cognitive dissonance, sunk cost, decision fatigue, risk aversion...)

3. XÁC ĐỊNH 2-4 NGUYÊN TẮC TÂM LÝ HỌC cụ thể đang tác động (Cialdini, Kahneman, Ariely, Thaler...)

4. XÂY DỰNG CHIẾN LƯỢC MULTI-STEP:
   - Mỗi bước phải gắn với 1 nguyên tắc tâm lý cụ thể
   - Phải có kỹ thuật dự phòng nếu bước trước thất bại
   - Không được chung chung kiểu "lắng nghe → đồng cảm → giải quyết"

5. SOẠN VÍ DỤ ĐỐI THOẠI:
   - Ví dụ TỐT: Tự nhiên như người thật nói, empathetic, có chiến lược ngầm
   - Ví dụ XẤU: Sai điển hình mà nhân viên thật hay mắc + giải thích TẠI SAO sai

QUY TẮC CỨNG (vi phạm = output bị loại):
- CẤM sáo rỗng: "xin lỗi vì sự bất tiện", "mong quý khách thông cảm", "sẽ cố gắng hết sức", "rất lấy làm tiếc"
- CẤM generic: Mỗi chiến lược PHẢI gắn nguyên tắc tâm lý cụ thể
- CẤM phóng đại ví dụ xấu — phải giống lỗi THẬT của nhân viên
- Tiếng Việt tự nhiên, mượt mà, giọng miền Nam hoặc trung tính

OUTPUT: Chỉ JSON đúng schema, không text khác."""
},

"user_single": {
"name": "User Prompt — Tạo 1 tình huống",
"desc": "Thay {TOPIC} bằng chủ đề cần tạo",
"prompt": """Tạo 1 tình huống CSKH/Sales chất lượng cao cho topic:
"{TOPIC}"

Output JSON theo schema:
{
  "id": "unique_id",
  "topic": "tên topic",
  "trigger": "1-2 câu trigger thực tế — khách nói gì?",
  "customer_psychology": {
    "surface": "cảm xúc bề mặt nhìn thấy được",
    "deep": "nỗi sợ / mong muốn tầng sâu",
    "hidden_motive": "động cơ ẩn — nguyên tắc tâm lý nào chi phối?"
  },
  "psychology_principles": ["nguyên tắc 1", "nguyên tắc 2", "nguyên tắc 3"],
  "strategy": {
    "objective": "mục tiêu chính của nhân viên",
    "tactics": [
      "Bước 1: [hành động cụ thể] — vì [nguyên tắc tâm lý]",
      "Bước 2: [hành động] — vì [nguyên tắc]",
      "Bước 3: [hành động] — vì [nguyên tắc]"
    ],
    "psychological_levers": ["lever 1", "lever 2"],
    "fallback": "nếu khách vẫn không hài lòng → làm gì?"
  },
  "good_example": "Nhân viên: [script đối thoại tự nhiên, empathetic, có chiến lược ngầm — ít nhất 3-4 câu]",
  "bad_example": "Nhân viên: [script sai điển hình — giống người thật sai, không phóng đại]",
  "bad_reason": "Giải thích 2-3 lý do tại sao ví dụ xấu thất bại (gắn nguyên tắc tâm lý)",
  "effectiveness_score": 8.5,
  "meta": {
    "scenario_type": "complaint|objection|hesitation|comparison|escalation",
    "difficulty": "easy|medium|hard|extreme",
    "industry": "retail|ecommerce|saas|banking|telecom|healthcare|education"
  }
}"""
},

"user_batch_5": {
"name": "User Prompt — Tạo 5 tình huống (batch)",
"desc": "Tạo 5 tình huống cùng lúc. Thay {DOMAIN} và {TOPICS}",
"prompt": """Tạo 5 tình huống CSKH/Sales chất lượng cao cho domain: {DOMAIN}

Topics:
1. {TOPIC_1}
2. {TOPIC_2}
3. {TOPIC_3}
4. {TOPIC_4}
5. {TOPIC_5}

Yêu cầu:
- Mỗi tình huống phải ĐỘC LẬP, không lặp pattern
- Psychology phải có 3 tầng (surface/deep/hidden_motive)
- Strategy phải gắn nguyên tắc tâm lý cụ thể
- Ví dụ tốt: tự nhiên, empathetic, chiến lược ngầm
- Ví dụ xấu: giống lỗi thật, giải thích tại sao sai
- KHÔNG sáo rỗng

Output: JSON array gồm 5 objects theo schema (mỗi object có đầy đủ các trường: id, topic, trigger, customer_psychology, psychology_principles, strategy, good_example, bad_example, bad_reason, effectiveness_score, meta)."""
},

"critic": {
"name": "Critique Prompt — Đánh giá chất lượng",
"desc": "Paste tình huống đã tạo vào {SCENARIO_JSON} để được chấm điểm",
"prompt": """Bạn là Giám khảo NGHIÊM KHẮC chấm chất lượng tình huống CSKH/Sales.
Đánh giá theo 5 tiêu chí (0-10), KHÔNG nể nang.

Tình huống cần đánh giá:
{SCENARIO_JSON}

Rubric chấm điểm:
1. psychology_depth (trọng số 30%): Tâm lý 3 tầng có insight thực sự? Có nguyên tắc tâm lý học cụ thể?
2. strategy_quality (trọng số 25%): Chiến lược multi-step? Mỗi bước gắn nguyên tắc tâm lý?
3. example_realism (trọng số 25%): Script đối thoại giống đời thật? Không sáo rỗng?
4. consistency (trọng số 10%): Trigger ↔ Psychology ↔ Strategy ↔ Examples nhất quán?
5. actionability (trọng số 10%): Nhân viên đọc xong có làm theo được ngay?

Kiểm tra RED FLAGS:
- Cụm sáo rỗng? ("xin lỗi vì sự bất tiện", "cố gắng hết sức"...)
- Psychology chỉ bề mặt? (chỉ "tức giận, buồn" mà thiếu phân tích sâu)
- Strategy generic? ("lắng nghe → đồng cảm → giải quyết")
- Ví dụ xấu phóng đại? (không giống lỗi thật)

Output JSON:
{
  "scores": {"psychology_depth": 0-10, "strategy_quality": 0-10, "example_realism": 0-10, "consistency": 0-10, "actionability": 0-10},
  "weighted_score": number,
  "red_flags": ["danh sách vấn đề"],
  "strengths": ["điểm mạnh"],
  "improvements": ["gợi ý cải thiện cụ thể"],
  "verdict": "EXCELLENT|GOOD|NEEDS_REVISION|REJECT"
}"""
},

"reviser": {
"name": "Revise Prompt — Cải thiện tình huống",
"desc": "Paste tình huống gốc + critique vào để cải thiện",
"prompt": """Bạn là Biên tập viên cấp cao. Cải thiện tình huống này để đạt >= 9.0/10 ở MỌI tiêu chí.

TÌNH HUỐNG GỐC:
{SCENARIO_JSON}

CRITIQUE:
{CRITIQUE_JSON}

Yêu cầu BẮT BUỘC:
- Sửa TẤT CẢ red flags và điểm yếu được chỉ ra
- Psychology phải có 3 tầng sâu với nguyên tắc tâm lý cụ thể
- Thay thế MỌI cụm sáo rỗng bằng hành động cụ thể
- Chiến lược phải multi-step, mỗi bước gắn nguyên tắc tâm lý
- Ví dụ tốt phải tự nhiên, empathetic, ít nhất 4-5 câu
- Ví dụ xấu phải giống lỗi thật, giải thích tại sao sai

Output: JSON đã cải thiện theo đúng schema gốc."""
},

"transcript_to_scenarios": {
"name": "Transcript → Tình huống (dùng sau khi export YouTube)",
"desc": "Paste transcript YouTube đã export vào {TRANSCRIPT} để trích xuất tình huống",
"prompt": """Bạn là chuyên gia phân tích nội dung đào tạo bán hàng.

Đọc transcript sau từ video YouTube về {DOMAIN}:

--- TRANSCRIPT ---
{TRANSCRIPT}
--- END TRANSCRIPT ---

Nhiệm vụ:
1. Tìm TẤT CẢ tình huống CSKH/Sales được nhắc đến trong transcript
2. Với mỗi tình huống, tạo structured data chất lượng cao
3. Bổ sung phân tích tâm lý 3 tầng (ngay cả khi video chỉ nói bề mặt)
4. Viết ví dụ đối thoại tốt/xấu dựa trên context từ video

Output: JSON array, mỗi phần tử theo schema:
{
  "source": "youtube_transcript",
  "source_context": "tóm tắt 1 câu phần transcript liên quan",
  "topic": "tên tình huống",
  "trigger": "khách nói/làm gì?",
  "customer_psychology": {
    "surface": "...",
    "deep": "...",
    "hidden_motive": "..."
  },
  "psychology_principles": ["..."],
  "strategy": {
    "objective": "...",
    "tactics": ["Bước 1: ...", "Bước 2: ...", "Bước 3: ..."],
    "psychological_levers": ["..."]
  },
  "good_example": "script tốt",
  "bad_example": "script xấu",
  "bad_reason": "tại sao xấu",
  "effectiveness_score": 8.5
}

Lưu ý: Extract NHIỀU tình huống nhất có thể. Mỗi tình huống phải độc lập, không lặp."""
},

"dataset_from_comments": {
"name": "Comment/Review → Tình huống",
"desc": "Paste comment Facebook/Shopee/Google Review vào để tạo tình huống",
"prompt": """Phân tích các comment/review khách hàng sau và tạo tình huống training:

--- COMMENTS ---
{COMMENTS}
--- END ---

Với MỖI comment có nội dung phàn nàn/hỏi/so sánh, tạo 1 tình huống theo schema JSON (trigger, customer_psychology 3 tầng, strategy multi-step, good/bad examples).

Ưu tiên comment có cảm xúc mạnh hoặc tình huống phức tạp.
Output JSON array."""
},

}

# ══════════════════════════════════════════════════════════
# EXPORT
# ══════════════════════════════════════════════════════════

def export_markdown(output_dir: str):
    """Export all prompts to a single markdown file."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    out = Path(output_dir) / "elite_prompts.md"

    lines = [
        "# Elite Prompts — Tạo Dataset CSKH/Sales Chất Lượng Cao",
        f"\nXuất lúc: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "\nDùng các prompt dưới đây với **Gemini Pro**, **Claude Sonnet 4.6**, **ChatGPT-4o**, hoặc bất kỳ chatbot nào.",
        "\n---\n",
    ]

    for key, data in PROMPTS.items():
        lines.append(f"## {data['name']}\n")
        lines.append(f"*{data['desc']}*\n")
        lines.append(f"```\n{data['prompt']}\n```\n")
        lines.append("---\n")

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ Exported to {out}")
    return str(out)


def export_individual(output_dir: str, fmt: str = "txt"):
    """Export each prompt to individual files."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    for key, data in PROMPTS.items():
        out = Path(output_dir) / f"{key}.{fmt}"
        out.write_text(data["prompt"], encoding="utf-8")
        print(f"  ✅ {out.name}")

    print(f"\n✅ {len(PROMPTS)} prompts exported to {output_dir}/")


def export_json(output_dir: str):
    """Export prompts as JSON (for API/UI use)."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    out = Path(output_dir) / "elite_prompts.json"
    out.write_text(json.dumps(PROMPTS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ Exported to {out}")
    return str(out)


def main():
    parser = argparse.ArgumentParser(description="Export elite prompts")
    parser.add_argument("--output", "-o", default="prompts_export/")
    parser.add_argument("--format", choices=["md", "txt", "json", "all"], default="all")
    args = parser.parse_args()

    if args.format in ("md", "all"):
        export_markdown(args.output)
    if args.format in ("txt", "all"):
        export_individual(args.output)
    if args.format in ("json", "all"):
        export_json(args.output)


if __name__ == "__main__":
    main()
