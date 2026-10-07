# Memory Agent — bộ nhớ dài hạn của Phidipus Agents

Lưu cục bộ trong một file SQLite (`data/memory/agent_memory.db`, quyền 600). Không có mã mạng
trong tầng lưu trữ.

## Mô hình
| Loại | Ví dụ | Nguồn |
|---|---|---|
| `profile` | "Tên tôi là Lan, kế toán" | người dùng |
| `preference` | "Sếp thích báo cáo PDF" | người dùng |
| `fact` | "Kho hàng chính ở Long An" | người dùng |
| `episode` | "Thành công: «tổng hợp tin tức» — cách: workflow:wf_news_digest" | tự động sau mỗi tác vụ |
| `procedure` | "Với yêu cầu kiểu …, cách hiệu quả: … (thành công 5/6)" | tự học |

## Ghi (kiểu Mem0)
Bộ lọc bí mật (API key, mật khẩu, OTP, số thẻ… → từ chối hoặc che) → trùng hoàn toàn: NOOP →
tìm ký ức tương tự → model local nhỏ quyết định **ADD / UPDATE (gộp) / INVALIDATE (thay thế) / NOOP**,
có luật dự phòng khi không có model. Ký ức bị thay thế **không xoá** mà ghi `valid_to` + `superseded_by`
(kiểu Zep) → luôn trả lời theo sự thật mới nhất nhưng vẫn xem được lịch sử.

## Đọc
Tìm kiếm lai: FTS5 BM25 (không dấu vẫn tìm được) + cosine embedding + độ mới + độ quan trọng.
`build_context()` ghép hồ sơ người dùng (khối "core memory" kiểu Letta) + ký ức liên quan để đưa vào
prompt của model **local**; chỉ gửi cho model đám mây khi bật `memory_agent.share_with_cloud`.

## Hợp nhất nền ("sleep-time")
Mỗi 6 giờ: dọn episode cũ, embedding lại khi đổi model, gộp ký ức trùng nghĩa, tổng hợp lại
khối hồ sơ/sở thích.

## Dùng
- Telegram: `/remember …` (`/nho`), `/recall …`, `/memories`, `/forget <id|từ khoá|all>`,
  `/memoryoff`, `/memoryon`, hoặc nhắn "ghi nhớ: …".
- Admin Panel → Bộ nhớ → Memory Agent; API `/api/v2/memory/agent/*`.
- MCP (Claude Desktop, Cursor…): `./venv/bin/python -m memory.mcp_server` — tools
  `memory_search`, `memory_add`, `memory_forget`, `memory_profile`, `memory_stats`.
