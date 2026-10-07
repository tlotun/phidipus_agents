# QwenElite PsySales Pipeline v1.0

## Tạo tình huống CSKH/Sales chất lượng cao — 100% local, miễn phí

Pipeline 3-pass **Generate → Critique → Revise (GCR)** + Rule Engine,
chạy hoàn toàn trên Mac qua Ollama.

---

## Cài đặt (1 lần)

```bash
# 1. Cài Ollama (nếu chưa có)
# Download từ https://ollama.com

# 2. Tải model (chọn 1 theo RAM của bạn)
ollama pull qwen3:72b     # 64GB RAM — chất lượng tốt nhất
ollama pull qwen3:32b     # 36GB RAM — cân bằng
ollama pull qwen3:8b      # 16GB RAM — nhanh, OK cho test

# 3. (Tùy chọn) Tạo model có sẵn system prompt elite
ollama create psysales-elite -f elite_pipeline/Modelfile
```

## Chạy nhanh

```bash
# Tạo 1 tình huống (test)
python elite_pipeline/run_pipeline.py \
  --topic "Khách hỏi giá ngay câu đầu tiên" \
  --model qwen3:72b

# Tạo batch 20+ tình huống
python elite_pipeline/run_pipeline.py \
  --topics-file elite_pipeline/topics.txt \
  --model qwen3:72b \
  --output training_data/elite.jsonl

# Nhanh hơn — bỏ GCR loop (1 pass duy nhất)
python elite_pipeline/run_pipeline.py \
  --topic "Khách chần chừ" --no-gcr --model qwen3:8b

# Dùng model nhỏ hơn (Mac M1 16GB)
python elite_pipeline/run_pipeline.py \
  --topics-file elite_pipeline/topics.txt \
  --model qwen3:8b --gcr-loops 1
```

## Pipeline hoạt động thế nào?

```
Topic: "Khách bực tức vì giao hàng trễ 3 ngày"
        │
        ▼
  ┌──────────────┐
  │  P1: Generate │  Qwen tạo tình huống đầy đủ (trigger + tâm lý 3 tầng
  │  (temp=0.65)  │  + chiến lược + ví dụ tốt/xấu) → JSON v1.0
  └──────┬───────┘
         │
    ┌────▼─────┐
    │ GCR Loop │ (lặp 2 lần)
    │          │
    │  ┌───────────┐
    │  │ Critique   │  Qwen đóng vai giám khảo, chấm 5 tiêu chí 0-10
    │  │ (temp=0.3) │  → PASS hoặc NEEDS_REVISION
    │  └─────┬─────┘
    │        │
    │  ┌─────▼─────┐
    │  │ Revise     │  Sửa theo critique → JSON v1.1, v1.2...
    │  │ (temp=0.65)│  (chỉ sửa phần yếu, không regenerate full)
    │  └───────────┘
    └──────┬───────┘
           │
    ┌──────▼───────┐
    │ Rule Engine   │  Kiểm tra: sáo rỗng? psychology nông?
    │ (code Python) │  strategy generic? ví dụ quá ngắn?
    └──────┬───────┘
           │
    ┌──────▼───────┐
    │ JSONL output  │  → training_data/elite.jsonl
    └──────────────┘
```

## Thời gian dự kiến (mỗi topic)

| Model      | RAM   | 1 pass | GCR 2 loop | Chất lượng |
|------------|-------|--------|------------|------------|
| qwen3:72b  | ~42GB | 2-4min | 8-15min    | ★★★★★     |
| qwen3:32b  | ~20GB | 1-2min | 4-8min     | ★★★★      |
| qwen3:8b   | ~5GB  | 15-30s | 1-2min     | ★★★       |

## Chất lượng kỳ vọng

| Cấu hình                 | So với Sonnet |
|--------------------------|---------------|
| qwen3:8b, không GCR      | ~55-60%       |
| qwen3:8b, GCR 2 loop     | ~65-70%       |
| qwen3:72b, không GCR     | ~68-72%       |
| qwen3:72b, GCR 2 loop    | ~75-80%       |
| qwen3:72b, GCR + fine-tune | ~82-88%     |

## Rule Engine kiểm tra gì?

1. **Anti-Cliché**: Phát hiện 10+ cụm sáo rỗng ("xin lỗi vì sự bất tiện", "cố gắng hết sức"...)
2. **Psychology Depth**: Kiểm tra tâm lý 3 tầng có đủ sâu không
3. **Strategy Quality**: Chiến lược có gắn nguyên tắc tâm lý cụ thể không
4. **Example Length**: Ví dụ đủ dài, tự nhiên, không trùng nhau
5. **Consistency**: Trigger ↔ Psychology ↔ Strategy nhất quán

## Output format (JSONL)

```json
{
  "id": "a1b2c3d4",
  "topic": "Khách hỏi giá ngay câu đầu tiên",
  "trigger": "Khách vào cửa hàng, chưa xem sản phẩm...",
  "customer_psychology": {
    "surface": "Sốt ruột, muốn biết ngay có phù hợp ngân sách không",
    "deep": "Sợ mất thời gian vào sản phẩm ngoài tầm chi trả",
    "hidden_motive": "Price anchoring — muốn neo giá trong đầu trước khi bị thuyết phục"
  },
  "psychology_principles": ["price anchoring", "loss aversion", "time scarcity"],
  "strategy": {
    "objective": "Chuyển focus từ giá sang giá trị",
    "tactics": ["Bước 1: Acknowledge nhu cầu biết giá...", "..."],
    "psychological_levers": ["anchoring reversal", "value framing"]
  },
  "good_example": "Dạ anh ơi, em báo giá ngay ạ. Dòng này có 3 mức...",
  "bad_example": "Dạ để em xem... Anh xem sản phẩm trước đi ạ.",
  "bad_reason": "Né tránh câu hỏi → khách cảm thấy bị manipulate",
  "effectiveness_score": 8.7,
  "version": "v1.2",
  "model": "qwen3:72b",
  "rule_engine_score": 9.0,
  "rule_engine_passed": true
}
```

## Tips

- **Mac Mini M1 16GB**: Dùng `qwen3:8b`, GCR 1 loop. Chất lượng ~65-70%.
- **Mac Mini M4 64GB**: Dùng `qwen3:72b`, GCR 2 loop. Chất lượng ~75-80%.
- **Muốn nhanh**: `--no-gcr` để chỉ generate, không critique/revise.
- **Muốn tốt nhất**: `--gcr-loops 3 --min-score 9.0` nhưng rất chậm.
- **Batch lớn**: Chạy qua đêm với `--topics-file topics.txt`.
