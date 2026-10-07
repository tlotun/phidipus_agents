#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
rag_engine/framework_store.py — Phidipus AI Forge E6
═══════════════════════════════════════════════════════════════

Framework Store: Domain-specific frameworks cho RAG injection.

Chứa các frameworks bán hàng, marketing, và VN-specific patterns.
Được inject vào context lúc inference để model nhỏ có thể dùng.

Nguyên tắc cốt lõi (Quy tắc #3):
  Model nhỏ (0.6B/1.7B) KHÔNG thể tự nghĩ chiến lược phức tạp.
  RAG inject framework vào context lúc inference.
  Không có RAG = chatbot chỉ nói xã giao.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Framework:
    id:          str
    name:        str
    domain:      str          # "sales", "health", "legal", "general"
    description: str
    steps:       list[str]
    vn_patterns: list[str]    # Vietnam-specific applications
    triggers:    list[str]    # Situation triggers khi dùng framework này
    example:     str


# ══════════════════════════════════════════════════════════════════
# SALES FRAMEWORKS
# ══════════════════════════════════════════════════════════════════

SPIN_SELLING = Framework(
    id="SPIN",
    name="SPIN Selling",
    domain="sales",
    description="Neil Rackham's consultative selling framework — phù hợp B2B và bán hàng giá trị cao",
    steps=[
        "S - Situation Questions: Hỏi về hoàn cảnh hiện tại của khách (không quá nhiều)",
        "P - Problem Questions: Khám phá vấn đề, khó khăn, sự không hài lòng",
        "I - Implication Questions: Hỏi về hậu quả của vấn đề nếu không giải quyết",
        "N - Need-Payoff Questions: Để khách tự nói ra giá trị của giải pháp",
    ],
    vn_patterns=[
        "Người Việt thường không muốn nói thẳng về vấn đề → dùng câu hỏi gián tiếp",
        "Bắt đầu bằng 'Anh/Chị thường xử lý X như thế nào?' thay vì 'Anh/Chị có vấn đề với X không?'",
        "Need-payoff: 'Nếu có thể X, điều đó sẽ giúp ích gì cho anh/chị?'",
    ],
    triggers=["need_discovery", "first_contact", "complex_sale", "b2b_sale"],
    example="'Hiện tại team của anh/chị xử lý [vấn đề] như thế nào? → Điều đó có gây ra khó khăn gì không? → Nếu không giải quyết, điều gì có thể xảy ra? → Nếu có giải pháp tốt hơn, điều đó sẽ giúp ích thế nào?'"
)

AIDA = Framework(
    id="AIDA",
    name="AIDA Framework",
    domain="sales",
    description="Attention → Interest → Desire → Action — phù hợp content marketing và pitch ngắn",
    steps=[
        "A - Attention: Thu hút sự chú ý bằng con số, câu hỏi gây tò mò, hoặc statement mạnh",
        "I - Interest: Giải thích relevance — tại sao điều này quan trọng với họ",
        "D - Desire: Kết nối với mong muốn cá nhân, show social proof",
        "A - Action: CTA rõ ràng, dễ làm, không áp lực",
    ],
    vn_patterns=[
        "Attention VN: Dùng con số thực tế từ thị trường Việt Nam",
        "Social proof: '90% doanh nghiệp SME ở Việt Nam đang gặp...'",
        "Action VN: 'Anh/chị thử dùng 7 ngày miễn phí' — không nói 'mua ngay'",
    ],
    triggers=["first_contact", "marketing_pitch", "cold_outreach", "content"],
    example="'80% doanh nghiệp [ngành] đang bỏ lỡ [cơ hội] vì... [Story ngắn] → Đây chính xác là vấn đề của anh/chị phải không? → [Testimonial] → Mình có thể setup demo 15 phút tuần này cho anh/chị xem thực tế không?'"
)

FEEL_FELT_FOUND = Framework(
    id="FEEL_FELT_FOUND",
    name="Feel-Felt-Found",
    domain="sales",
    description="Empathy framework cho objection handling — đặc biệt hiệu quả với người Việt",
    steps=[
        "Feel: 'Tôi hiểu anh/chị cảm thấy...' — Acknowledge cảm xúc, không phản bác",
        "Felt: 'Nhiều khách hàng của tôi cũng từng thấy như vậy...' — Social proof",
        "Found: 'Nhưng họ đã phát hiện ra rằng...' — Reframe và evidence",
    ],
    vn_patterns=[
        "VN đặc biệt: Người Việt coi trọng 'mặt mũi' → không bao giờ nói 'anh/chị sai'",
        "Feel VN: 'Anh/chị nói điều này tôi hoàn toàn hiểu được...'",
        "Found VN: Dùng case study từ khách hàng tương tự trong cùng ngành/khu vực",
    ],
    triggers=["price_objection", "not_interested", "competitor_comparison", "need_to_think"],
    example="'Tôi hiểu anh/chị thấy giá có vẻ cao so với ngân sách. Nhiều khách hàng của tôi, đặc biệt [doanh nghiệp tương tự], ban đầu cũng có cảm giác tương tự. Nhưng sau khi dùng 3 tháng, họ thấy rằng chi phí thực tế chỉ bằng [X]% so với lợi ích mang lại...'"
)

# Vietnam-specific patterns
VN_GIA_REF = Framework(
    id="VN_GIA_REF",
    name="VN: Reflex Hỏi Giá",
    domain="sales",
    description="Xử lý khi khách hỏi giá ngay câu đầu — văn hóa đặc trưng Việt Nam",
    steps=[
        "Không báo giá ngay — chuyển sang khám phá nhu cầu",
        "Acknowledge câu hỏi giá: 'Tôi sẽ chia sẻ ngay về chi phí'",
        "Bridge: 'Nhưng để tư vấn đúng nhất, cho tôi hỏi nhanh...'",
        "1-2 câu hỏi nhu cầu → sau đó mới discuss về giá trị trước giá cả",
    ],
    vn_patterns=[
        "Người Việt hỏi giá = habit, không phải chỉ quan tâm đến giá",
        "Nếu báo giá sớm → mất context → khó upsell/justify value",
        "Bridge chuẩn: 'Mình muốn đảm bảo báo giá đúng với nhu cầu của anh/chị...'",
    ],
    triggers=["price_question_early", "first_contact"],
    example="'Giá của mình khá linh hoạt tùy theo nhu cầu thực tế. Để báo đúng nhất, anh/chị đang cần giải quyết vấn đề gì cụ thể? Quy mô team đang là bao nhiêu người? → [sau khi hiểu nhu cầu] Với yêu cầu như vậy, giải pháp phù hợp nhất là...'"
)

VN_GIA_DINH = Framework(
    id="VN_GIA_DINH",
    name="VN: Tham Khảo Gia Đình",
    domain="sales",
    description="Xử lý khi khách cần hỏi ý kiến người thân/vợ/chồng/đối tác",
    steps=[
        "Không ép — respect quyết định tham khảo (quan trọng với văn hóa VN)",
        "Hỗ trợ quá trình thuyết phục: cung cấp tài liệu, brochure, video ngắn",
        "Offer: 'Tôi có thể cùng gặp cả gia đình/đối tác không?'",
        "Follow-up cụ thể: 'Khi nào anh/chị discuss xong, mình có thể nói chuyện lại vào thứ...'",
    ],
    vn_patterns=[
        "Đây là quyết định thực sự, không phải từ chối — xử lý khác với objection",
        "Cung cấp 'ammunition' cho khách để thuyết phục người thân",
        "Đề xuất gặp cùng: 'Anh/chị thấy có tiện không nếu mình gặp cả nhà 30 phút?'",
    ],
    triggers=["ask_family", "need_to_think", "joint_decision"],
    example="'Hoàn toàn đúng khi anh/chị muốn hội ý với gia đình — đây là quyết định quan trọng. Để giúp cuộc trao đổi thuận tiện hơn, mình sẽ gửi anh/chị tài liệu tóm tắt và video demo ngắn. Thứ mấy tuần này mình có thể liên lạc lại để xem anh/chị thảo luận như thế nào?'"
)


# ══════════════════════════════════════════════════════════════════
# HEALTH FRAMEWORKS
# ══════════════════════════════════════════════════════════════════

SYMPTOM_GUIDE = Framework(
    id="SYMPTOM_GUIDE",
    name="Symptom Information Guide",
    domain="health",
    description="Framework cung cấp thông tin triệu chứng an toàn — không chẩn đoán",
    steps=[
        "Acknowledge: Xác nhận concern của người dùng",
        "General info: Cung cấp thông tin chung về triệu chứng (giáo dục)",
        "Red flags: Nêu dấu hiệu cần gặp bác sĩ ngay",
        "Redirect: Luôn khuyên tham khảo chuyên gia y tế",
    ],
    vn_patterns=[
        "Người Việt hay tự chữa bệnh → giáo dục về rủi ro nhẹ nhàng",
        "Red flags VN: 'Nếu triệu chứng kéo dài > 3 ngày hoặc nặng hơn → gặp bác sĩ ngay'",
        "Không dùng từ 'có thể bị X' — dùng 'có thể liên quan đến nhiều nguyên nhân'",
    ],
    triggers=["symptom_inquiry", "medication_question", "health_concern"],
    example="'Triệu chứng anh/chị mô tả có thể liên quan đến nhiều nguyên nhân khác nhau. Để được chẩn đoán chính xác, cần được bác sĩ khám trực tiếp. Một số dấu hiệu cần đến cơ sở y tế ngay là... [list red flags]. Trong lúc chờ khám, anh/chị có thể...'"
)

CRISIS_PROTOCOL = Framework(
    id="CRISIS_PROTOCOL",
    name="Crisis Detection & Redirect",
    domain="psychology",
    description="Protocol xử lý khủng hoảng tâm lý — redirect ngay đến chuyên gia",
    steps=[
        "Detect: Nhận diện ngôn ngữ khủng hoảng (tự làm hại, vô vọng, tuyệt vọng)",
        "Acknowledge: Không phủ nhận cảm xúc — 'Tôi nghe thấy bạn đang rất khó khăn'",
        "Redirect NGAY: Cung cấp đường dây hỗ trợ khủng hoảng",
        "Stay present: Không kết thúc conversation đột ngột",
    ],
    vn_patterns=[
        "Đường dây hỗ trợ VN: 1800 599 920 (miễn phí, 24/7)",
        "Không phán xét, không thách thức, không minimize",
        "Nếu có nguy hiểm ngay: hướng dẫn gọi 115",
    ],
    triggers=["crisis_detection", "self_harm", "suicidal_ideation", "extreme_distress"],
    example="'Tôi nghe thấy bạn đang trải qua giai đoạn rất khó khăn. Điều đó hoàn toàn có thể. Bạn không phải đối mặt với điều này một mình — xin hãy gọi đường dây hỗ trợ tâm lý miễn phí 1800 599 920 (24/7). Họ có chuyên gia có thể giúp bạn ngay bây giờ.'"
)

CBT_BASICS = Framework(
    id="CBT_BASICS",
    name="CBT Basic Techniques",
    domain="psychology",
    description="Kỹ thuật CBT cơ bản cho coaching — không phải trị liệu",
    steps=[
        "Identify: Nhận diện thought pattern tiêu cực",
        "Challenge: Đặt câu hỏi thách thức: 'Bằng chứng nào cho điều đó?'",
        "Reframe: Tìm cách nhìn khác thực tế hơn",
        "Action: Bước nhỏ có thể làm ngay",
    ],
    vn_patterns=[
        "VN: Nhiều người chưa quen với therapy language → dùng ngôn ngữ đời thường",
        "Tránh: 'Bạn cần cognitive restructuring' → Dùng: 'Hãy thử nhìn theo cách khác'",
    ],
    triggers=["negative_thinking", "anxiety_basic", "low_self_esteem", "stress_management"],
    example="'Khi bạn nghĩ rằng X, điều gì khiến bạn chắc chắn điều đó là đúng? [Pause] Có trường hợp nào ngược lại không? [Explore] Nếu X không hoàn toàn đúng, bạn sẽ nhìn tình huống này như thế nào?'"
)


# ══════════════════════════════════════════════════════════════════
# Framework Registry
# ══════════════════════════════════════════════════════════════════

ALL_FRAMEWORKS: dict[str, Framework] = {
    "SPIN":             SPIN_SELLING,
    "AIDA":             AIDA,
    "FEEL_FELT_FOUND":  FEEL_FELT_FOUND,
    "VN_GIA_REF":       VN_GIA_REF,
    "VN_GIA_DINH":      VN_GIA_DINH,
    "SYMPTOM_GUIDE":    SYMPTOM_GUIDE,
    "CRISIS_PROTOCOL":  CRISIS_PROTOCOL,
    "CBT_BASICS":       CBT_BASICS,
}


def get_framework(framework_id: str) -> Optional[Framework]:
    return ALL_FRAMEWORKS.get(framework_id)


def get_domain_frameworks(domain_config: dict) -> list[Framework]:
    """Lấy frameworks được config cho domain."""
    rag_cfg = domain_config.get("rag", {})
    framework_ids = rag_cfg.get("frameworks", [])
    return [ALL_FRAMEWORKS[fid] for fid in framework_ids if fid in ALL_FRAMEWORKS]


def format_framework_for_rag(framework: Framework, situation: str = "") -> str:
    """Format framework thành text để inject vào RAG context."""
    steps_str = "\n".join(f"  {s}" for s in framework.steps)
    vn_str = "\n".join(f"  • {p}" for p in framework.vn_patterns[:2])

    text = f"""[FRAMEWORK: {framework.name}]
{framework.description}

Các bước:
{steps_str}

Áp dụng trong văn hóa Việt Nam:
{vn_str}

Ví dụ: {framework.example[:200]}"""

    return text


def get_relevant_frameworks(
    situation_type: str,
    domain_config: dict,
    top_k: int = 2,
) -> list[Framework]:
    """
    Lấy frameworks phù hợp nhất với situation type.
    Simple keyword matching — thay bằng vector search trong production.
    """
    domain_frameworks = get_domain_frameworks(domain_config)
    scored = []

    for fw in domain_frameworks:
        score = 0
        if situation_type in fw.triggers:
            score += 10
        for trigger in fw.triggers:
            if trigger in situation_type or situation_type in trigger:
                score += 5
        if score > 0:
            scored.append((score, fw))

    scored.sort(key=lambda x: -x[0])
    return [fw for _, fw in scored[:top_k]]
