# Changelog

## 4.3.1 — 2026-10

### Bảo mật
- Nâng cấp thư viện có lỗ hổng đã công bố (Dependabot): aiohttp 3.14.4, Pillow 12.3.0, cryptography 50.0.2;
  gói tuỳ chọn: transformers 5.10.2, sentence-transformers 5.7.0, datasets 5.0.1, yt-dlp 2026.8.19;
  giao diện Admin (chỉ lúc phát triển): vite 6.4.4.
- chromadb chưa có bản vá; các lỗ hổng chỉ ảnh hưởng máy chủ HTTP của Chroma, Phidipus Agents dùng
  `PersistentClient` nhúng nên không bị ảnh hưởng (ghi chú trong `requirements/rag.txt`).
- `tools/check_secrets.py` quét cả file test để chặn chuỗi có định dạng khoá thật (kể cả khoá giả).

### Sửa lỗi
- Telegram: `telegram.telegram_config` luôn import được, bất kể bot được nạp trước hay sau.
- Mã nguồn giao diện Admin build lại được (thẻ `</template>` thừa trong `AILabView.vue`).

### Đóng góp
- CONTRIBUTING viết lại; mẫu issue (báo lỗi, đề xuất, phím tắt, hỏi đáp) và mẫu pull request có CLA.
- `tools/try_command.py`: chạy thử một lệnh với danh mục phím tắt, không cần quyền macOS.

## 4.3.0 — 2026-10

### Bảo mật
- Admin panel: middleware chặn DNS-rebinding (Host), CSRF và chiếm WebSocket từ trang web lạ (Origin);
  bỏ tài khoản mặc định `admin/phidipus`; mật khẩu scrypt; tunnel không còn được coi là "localhost".
- ReAct không còn tự chạy lại tác vụ có tác dụng phụ (gửi email, đăng bài, xoá…).
- Lệnh bị từ chối / quá rủi ro dừng hẳn, không chuyển xuống tầng AI khác.
- Gemini API key chuyển từ URL sang header; model Gemini đã ngừng (1.5/2.0/preview) tự đổi sang 2.5.
- Giấy phép thương mại: kiểm tra offline bằng chữ ký Ed25519 (bỏ server giả + vân tay thiết bị).

### Sửa lỗi lớn
- IPC: kết quả `success` phản ánh đúng L2; 21 payload sai tên/kiểu; L2 Quartz thật thay stub.
- Định tuyến workflow: lệnh ngắn ("mở", "báo cáo") không còn chạy nhầm workflow; hỗ trợ không dấu.
- Brain v7: chạy in-process (nạp model 1 lần), đúng chat template, không còn "clarify" cụt khi lỗi.
- Telegram: gửi text/file tách riêng, huỷ tác vụ thật, xác nhận thao tác nguy hiểm, chia tin dài.
- Scheduler: hỗ trợ `workflow_file`, chạy theo chu kỳ; lịch đăng bài có đủ IPC/LLM.
- Toạ độ VLM được quy đổi một lần (đồng nhất với Accessibility); `mouse_drag` không còn bị bỏ sót.

### Tính năng mới
- **Memory Agent** (SQLite + FTS5 + embedding, ADD/UPDATE/INVALIDATE/NOOP, lịch sử theo thời gian,
  hợp nhất nền, MCP server, lệnh Telegram, tab Admin).
- **Keyboard-first**: kho ~200 phím tắt macOS + macro, `ui_snapshot` / `menu_list` / `menu_select`,
  node workflow `shortcut` / `menu` / `macro`, lazy vision.
- **Tool calling gốc** cho agent (Ollama `tools`), kiểm soát `think`, structured output.
- **Model registry** đời mới (Qwen3.5, Gemma 4, Qwen3.6/3.8, qwen3-embedding…), vai trò `heavy`,
  nhận biết năng lực model qua `/api/show`.
- **Trình cài đặt 1 lần bấm**: `install.sh` / `Cai_Dat_Phidipus.command` (uv, Python 3.12, Ollama,
  model theo RAM, Brain, quyền, `Phidipus Agents.app`, `--doctor`), gỡ cài đặt an toàn.
- Phát hành: `.gitignore`, `config.example.yaml`, quét bí mật + pre-commit hook, header SPDX.
