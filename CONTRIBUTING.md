# Đóng góp cho Phidipus Agents

Cảm ơn bạn đã muốn góp sức! Tài liệu này giúp bạn gửi đóng góp đúng ngay từ lần đầu.
*English summary at the bottom.*

## Trước khi bắt đầu

- **Lỗ hổng bảo mật**: đừng mở issue công khai — làm theo [SECURITY.md](SECURITY.md).
- **Thay đổi lớn** (tính năng mới, đổi kiến trúc, thêm thư viện): mở issue **Đề xuất tính năng** trước để
  thống nhất hướng đi, tránh mất công viết code không được nhận.
- **Giấy phép**: Phidipus Agents dùng PolyForm Noncommercial 1.0.0 và có giấy phép thương mại riêng
  ([COMMERCIAL.md](COMMERCIAL.md)), nên mỗi người đóng góp cần đồng ý [CLA](CLA.md) một lần
  (xem [Gửi pull request](#gửi-pull-request)). Bạn vẫn giữ bản quyền phần mình viết.
- **Ứng xử**: tôn trọng, lịch sự; góp ý vào vấn đề, không nhắm vào con người.

## Bạn có thể đóng góp gì

| Loại | Độ khó | Bắt đầu từ đâu |
|---|---|---|
| Báo lỗi | dễ | mẫu **Báo lỗi** khi tạo issue |
| Thêm phím tắt / hỗ trợ ứng dụng mới | dễ — chỉ sửa YAML | [Thêm phím tắt hoặc ứng dụng](#thêm-phím-tắt-hoặc-ứng-dụng) |
| Workflow mẫu | trung bình | [Thêm workflow mẫu](#thêm-workflow-mẫu) |
| Tài liệu, bản dịch tiếng Anh | dễ | `README.md`, `docs/` |
| Sửa lỗi, tính năng mới | tuỳ việc | issue gắn nhãn `good first issue` hoặc `help wanted` |

## Cài môi trường phát triển

Cần macOS 13 trở lên (khuyến nghị Apple Silicon). Không cần Homebrew hay `sudo`.

```bash
# 1. Fork repo trên GitHub, rồi:
git clone https://github.com/<tên-bạn>/phidipus_agents.git
cd phidipus_agents

# 2. Python 3.12 + thư viện vào ./venv, tạo cấu hình — không tải Ollama/model
./install.sh --only config

# 3. Bật hook chặn lộ bí mật trước mỗi commit
./venv/bin/python tools/check_secrets.py --install-hook
```

Muốn chạy agent thật: chạy `./install.sh` đầy đủ (tự chọn model theo RAM; máy 8 GB dùng `--profile lite`),
cấp quyền **Accessibility** và **Screen Recording** cho Terminal, rồi:

```bash
./venv/bin/python main.py --no-telegram   # agent + Admin Panel tại http://127.0.0.1:8912
./venv/bin/python main.py --demo          # chỉ giao diện, không cần Ollama
```

### Kiểm tra trước khi gửi

```bash
./venv/bin/python -m unittest discover -s tests -t .   # không cần Ollama, quyền macOS hay mạng
./venv/bin/python tools/check_secrets.py                # không token, khoá, dữ liệu cá nhân
./venv/bin/python installer/setup_phidipus.py --doctor  # (tuỳ chọn) sức khoẻ bản cài
```

## Bản đồ mã nguồn

| Thư mục | Vai trò |
|---|---|
| `main.py` | Điểm khởi động: AgentLoop + Admin Panel + Telegram |
| `core/` | AgentLoop (L1), keyboard-first, model registry, router, workflow executor |
| `planner/` | ReAct agent — gọi công cụ gốc (native tool calling) |
| `ipc/` | Kênh L1 ↔ L2: Unix socket, HMAC, kiểm tra schema |
| `automation/` | Thao tác macOS phía L2: Quartz, Accessibility, AppleScript, Chrome |
| `vision/` | Đọc màn hình bằng model nhìn (VLM) — chỉ dùng khi phím tắt/menu không đủ |
| `memory/` | Memory Agent (SQLite + FTS5 + embeddings), bộ lọc bí mật, MCP server |
| `skills/`, `sandbox/`, `skill_validator/` | Skill Forge: sinh kỹ năng mới và chạy trong sandbox |
| `admin/` | Admin Panel: FastAPI (`admin_server_v2.py`) + giao diện Vue (`admin/ui`) |
| `telegram/` | Telegram bot |
| `data/shortcuts/macos.yaml` | Danh mục phím tắt, menu, macro |
| `data/workflows/` | Workflow mẫu |
| `installer/`, `install.sh` | Trình cài đặt một lần bấm |
| `tests/`, `tools/` | Test và công cụ cho người phát triển |

Đọc thêm: [kiến trúc](README.md#kiến-trúc-tóm-tắt) · [docs/KEYBOARD_FIRST.md](docs/KEYBOARD_FIRST.md) ·
[docs/MEMORY_AGENT.md](docs/MEMORY_AGENT.md).

## Quy tắc viết mã

- **Giống code xung quanh**: Python 3.12, type hint; comment và tên biến bằng tiếng Anh;
  **thông báo cho người dùng bằng tiếng Việt**.
- **File mới** mở đầu bằng:
  ```python
  # SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
  # Copyright (c) 2026 Phidipus Agents (see NOTICE)
  ```
- **An toàn trước hết**
  - Mọi thao tác chuột / bàn phím / Accessibility đi qua IPC tới L2 — không gọi Quartz hay AppleScript trực tiếp từ L1.
  - Thao tác không hoàn tác được (xoá, gửi, thanh toán, thoát ứng dụng…) phải có `risk: high` hoặc hỏi xác nhận.
  - Dịch vụ chỉ lắng nghe trên `127.0.0.1`; không thêm tài khoản/mật khẩu mặc định; không telemetry.
- **Test**: mỗi bản sửa lỗi kèm một test tái hiện lỗi đó. Test không được gọi mạng, Ollama hay cần quyền macOS
  (dùng lớp giả, ví dụ `FakeEmbedder` trong `tests/test_memory_agent.py`).
- **Khoá giả trong test**: ghép chuỗi lúc chạy, ví dụ `"AIza" + "Sy" + "x" * 33`. Viết liền một chuỗi giống khoá thật
  thì GitHub sẽ cảnh báo công khai dù là khoá giả — `tools/check_secrets.py` chặn trường hợp này.
- **Không commit**: `config.yaml`, `keys/`, token bot, dữ liệu trong `data/memory/`, model, adapter, đường dẫn
  `/Users/<tên>/`. `.gitignore` và hook đã chặn sẵn — đừng tắt chúng.

## Thêm phím tắt hoặc ứng dụng

Đây là cách đóng góp dễ và có ích nhất: lệnh chạy bằng phím tắt hoặc menu nhanh hơn hàng trăm lần so với
"chụp màn hình → AI đoán chỗ click", và không bao giờ click nhầm.

1. Mở `data/shortcuts/macos.yaml` — đầu file giải thích đủ các trường.
2. Ứng dụng mới: thêm vào `apps:`; lấy bundle id bằng `osascript -e 'id of app "Tên App"'`.
3. Thêm phím tắt vào nhóm (`groups:`) của ứng dụng:
   ```yaml
   - {id: new_tab, keys: "cmd+t", desc: "New tab", say: [mở tab mới, tab mới, new tab], expect: window}
   ```
   - `say`: câu lệnh tiếng Việt **và** tiếng Anh (không phân biệt dấu, khớp cả câu).
   - `risk: high` cho thao tác nguy hiểm; `menu:` là đường dẫn menu dự phòng (ghi cả tên tiếng Anh và tiếng Việt).
   - Chuỗi thao tác có tham số (`{path}`, `{query}`…) → khai báo trong `macros:`.
4. Thử ngay, không cần quyền macOS hay Ollama:
   ```bash
   ./venv/bin/python tools/try_command.py --app chrome "mở tab mới"
   ./venv/bin/python -m unittest tests.test_keyboard_first
   ```
5. Nếu được, thử thật khi agent đang chạy và ghi phiên bản macOS + ứng dụng vào PR.

## Thêm workflow mẫu

1. Sao chép một file gần giống trong `data/workflows/` (ví dụ `13_tong_hop_tin_tuc.json`), đặt tên `NN_ten_ngan.json`.
2. Đổi `id`, `name`, `trigger_phrases`; mỗi node gồm `id`, `type`, `config` và `next`.
3. Không ghi dữ liệu thật (email, số điện thoại, link Google Sheet riêng) — dùng giá trị ví dụ.
4. Chạy thử trong Admin Panel → tab **Workflow**, hoặc gửi một câu trong `trigger_phrases` qua Telegram.

## Gửi pull request

1. Tạo nhánh từ `main`: `fix/ten-loi` hoặc `feat/ten-tinh-nang`.
2. Commit nhỏ, mỗi commit một việc; nội dung commit tiếng Việt hay tiếng Anh đều được.
3. Chạy [kiểm tra](#kiểm-tra-trước-khi-gửi) — tất cả phải đạt.
4. Mở PR và điền theo mẫu. Lần đầu đóng góp, đánh dấu ô CLA:
   `I have read and agree to the Phidipus Agents CLA (CLA.md).`
5. Thay đổi giao diện (Admin Panel, tin nhắn Telegram) → kèm ảnh chụp.

Chủ dự án sẽ review và có thể đề nghị chỉnh sửa trước khi gộp.

## Báo lỗi hiệu quả

Dùng mẫu **Báo lỗi** và gửi kèm: phiên bản, macOS + chip + RAM, lệnh đã gửi, kết quả mong đợi và thực tế,
kết quả `--doctor`, đoạn log liên quan. **Xoá token, API key, mật khẩu, số điện thoại, email** trước khi dán.

---

## English summary

- **Security issues**: never open a public issue — see [SECURITY.md](SECURITY.md).
- **Large changes**: open a feature request first.
- **License & CLA**: Phidipus Agents is source-available (PolyForm Noncommercial 1.0.0) with separate commercial
  licenses, so every contributor agrees to the [CLA](CLA.md) once, by ticking the CLA box in the pull request template.
- **Setup**: `./install.sh --only config`, then `./venv/bin/python tools/check_secrets.py --install-hook`.
- **Before a pull request**: `./venv/bin/python -m unittest discover -s tests -t .` and
  `./venv/bin/python tools/check_secrets.py` must pass.
- **Rules**: match the surrounding code; user-facing text in Vietnamese; every mouse/keyboard/Accessibility action
  goes through IPC to the L2 daemon; irreversible actions need `risk: high` or a confirmation; tests must not use
  the network, Ollama or macOS permissions; build fake credentials at runtime.
- **Easiest contribution**: new shortcuts in `data/shortcuts/macos.yaml` — try them with `tools/try_command.py`.
