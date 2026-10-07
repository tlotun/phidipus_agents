# 🕷️ Phidipus Agents — trợ lý AI tự thao tác trên máy Mac (chạy local)

Phidipus Agents nhận lệnh tiếng Việt (qua Telegram hoặc Admin Panel) và tự làm việc trên macOS:
mở ứng dụng, duyệt web, xử lý file, email, báo cáo, chạy workflow theo lịch — bằng các model AI
chạy ngay trên máy bạn, dữ liệu không rời khỏi máy.

> **Source-available** · [PolyForm Noncommercial 1.0.0](LICENSE) · miễn phí cho cá nhân, học tập,
> nghiên cứu, phi lợi nhuận — **dùng thương mại cần giấy phép** ([COMMERCIAL.md](COMMERCIAL.md)).

<!-- demo:start -->
<!-- demo:end -->

## Điểm nổi bật (v4.3)

| | |
|---|---|
| ⌨️ **Keyboard-first** | 200+ phím tắt macOS theo từng ứng dụng, đọc và bấm **menu qua Accessibility** (đúng cả giao diện tiếng Việt), macro có tham số. Chỉ dùng "nhìn màn hình" (VLM) khi thật cần → nhanh hơn, chính xác hơn, nhẹ máy hơn. |
| 🧠 **Memory Agent** | Ghi nhớ thông tin, sở thích, cách làm hiệu quả; tự gộp/thay thế ký ức cũ (giữ lịch sử); tìm kiếm tiếng Việt có hoặc không dấu; tự tổng hợp hồ sơ người dùng; có **MCP server** để Claude Desktop / Cursor dùng chung. |
| 🤖 **Model local đời mới** | Qwen3.5 / Gemma 4 / Qwen3.6 / Qwen3.8 — đa phương thức + gọi công cụ: một model vừa suy luận vừa đọc màn hình. Trình cài đặt tự chọn theo RAM. |
| 🛠️ **Tool calling gốc** | Vòng lặp agent dùng function-calling thay cho phân tích văn bản; tự sửa khi gọi sai tham số; hỏi xác nhận trước thao tác không hoàn tác được. |
| 📱 **Telegram** | Điều khiển từ xa, nhận file/ảnh kết quả, xác nhận thao tác nguy hiểm, lệnh `/remember` `/recall`… |
| 🔁 **Workflow** | 15 workflow mẫu (báo cáo doanh số, theo dõi giá đối thủ, tóm tắt email…), dạy workflow mới, chạy theo lịch. |
| 🔒 **An toàn mặc định** | Chỉ chạy trên localhost, không tài khoản mặc định, chống CSRF / DNS-rebinding / chiếm WebSocket, không telemetry, bí mật không bao giờ được lưu vào bộ nhớ. |

## Yêu cầu

- macOS 13 trở lên · RAM từ 8 GB (khuyến nghị 16 GB+) · trống 15–40 GB cho model AI.
- Brain v7 (tầng phân loại MLX) cần Apple Silicon + macOS 14+. Mac Intel vẫn chạy được phần còn lại.

## Cài đặt — một lần bấm

**Cách 1 — dán một dòng vào Terminal:**

```bash
curl -fsSL https://raw.githubusercontent.com/tlotun/phidipus_agents/main/install.sh | bash
```

**Cách 2 — tải ZIP từ GitHub:** giải nén → bấm đúp **`Cai_Dat_Phidipus.command`**
(lần đầu macOS có thể hỏi: chuột phải → *Mở*).

Trình cài đặt không cần `sudo` hay Homebrew. Nó tự làm:
uv (có kiểm tra checksum) → Python 3.12 → thư viện → Ollama (kiểm tra chữ ký Apple) →
model hợp với RAM → khoá bảo mật → `config.yaml` → biểu tượng **Phidipus Agents.app** → quyền macOS → kiểm tra tổng thể.
Chạy lại bất cứ lúc nào để cập nhật hoặc sửa lỗi: bước đã xong sẽ được bỏ qua, tải dở sẽ tải tiếp.

| RAM | Model chính (suy luận + nhìn) | Model nhanh | Tuỳ chọn thêm |
|---|---|---|---|
| 8 GB | `qwen3.5:4b` | `qwen3.5:4b` | — |
| 16–24 GB | `qwen3.5:9b` | `qwen3.5:4b` | — |
| 32–48 GB | `qwen3.5:9b` | `qwen3.5:4b` | `gemma4:26b-a4b`, `qwen3-coder:30b` |
| 64 GB+ | `qwen3.6:35b-a3b` | `qwen3.5:4b` | `qwen3.8:27b`, `qwen3-coder:30b` |

Embedding: `qwen3-embedding:0.6b` (bộ nhớ) + `nomic-embed-text` (RAG). Thiếu model nào, Phidipus Agents
tự dùng model tương đương đã có (`core/model_registry.py`).

## Sử dụng

1. Mở **Phidipus Agents** bằng Spotlight (hoặc bấm đúp `Phidipus_Agent.command`).
2. Cấp quyền **Accessibility** và **Screen Recording** cho Terminal (trình cài đặt mở sẵn đúng mục).
3. Admin Panel: <http://127.0.0.1:8912> — thêm token Telegram, API key (tuỳ chọn), xem bộ nhớ, workflow.
4. Ví dụ lệnh: `mở tab mới trong chrome` · `xuất pdf` · `đi tới thư mục ~/Downloads` ·
   `tổng hợp tin tức về AI` · `ghi nhớ: sếp Minh thích báo cáo dạng PDF` · `/recall sếp thích gì`.

Kiểm tra sức khoẻ: `./venv/bin/python installer/setup_phidipus.py --doctor`
Gỡ cài đặt: bấm đúp `Go_Cai_Dat_Phidipus.command` — dữ liệu cá nhân chỉ bị xoá khi bạn đồng ý, và được chuyển vào Thùng rác.

## Kiến trúc tóm tắt

```
Telegram / Admin Panel ─► AgentLoop (L1)
   SmartAction ─► Keyboard-first (phím tắt · menu · macro) ─► Brain v7 ─► Router/Workflow
   ─► Skill Forge (sandbox) ─► ReAct agent (tool calling, nhìn màn hình khi cần)
        │  Memory Agent (SQLite + FTS5 + embeddings)   Model registry (Ollama)
        ▼
   IPC (Unix socket, HMAC, schema) ─► Daemon L2: Quartz · Accessibility · AppleScript
```

Chi tiết: [docs/KEYBOARD_FIRST.md](docs/KEYBOARD_FIRST.md) · [docs/MEMORY_AGENT.md](docs/MEMORY_AGENT.md).

## Giấy phép & nhãn hiệu

- Mã nguồn: **PolyForm Noncommercial 1.0.0** — xem [LICENSE](LICENSE). Đây là giấy phép
  *source-available* (không phải OSI open source).
- Dùng thương mại: [COMMERCIAL.md](COMMERCIAL.md) — giấy phép ký số, kiểm tra offline.
- Tên và logo Phidipus Agents: [TRADEMARKS.md](TRADEMARKS.md). Thành phần bên thứ ba: [NOTICE](NOTICE).

Đóng góp: [CONTRIBUTING.md](CONTRIBUTING.md) (cần đồng ý [CLA](CLA.md)) · Bảo mật: [SECURITY.md](SECURITY.md).

---

### English

Phidipus Agents is a local-first AI agent that operates a Mac from Vietnamese (or English) instructions
sent through Telegram or a local admin panel. It prefers exact keyboard shortcuts and menu commands
(Accessibility) over screenshot-and-click, keeps a local long-term memory (also exposed as an MCP
server), and runs current local models (Qwen3.5, Gemma 4, Qwen3.6/3.8) through Ollama with native
tool calling. Install with the one-liner above. Source-available under PolyForm Noncommercial 1.0.0;
commercial use requires a license (see COMMERCIAL.md).
