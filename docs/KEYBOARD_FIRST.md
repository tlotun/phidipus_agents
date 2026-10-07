# Keyboard-first — thay "nhìn màn hình rồi click" bằng phím tắt, menu và Accessibility

## Vấn đề
Cách cũ cho mỗi bước: chụp màn hình → VLM đoán vị trí → click toạ độ. Mỗi bước 1.5–25 giây,
cần model nhìn ảnh chiếm RAM, và hỏng khi giao diện xê dịch, đổi ngôn ngữ, đổi độ phân giải.
Phần lớn lệnh trên macOS đã có dạng chính xác và tức thì: phím tắt hoặc mục menu.

## Thứ tự ưu tiên (rẻ & chắc → đắt)
1. **Phím tắt** trong kho `data/shortcuts/macos.yaml` (theo app đang mở).
2. **Menu bar qua Accessibility** — `menu_list` đọc toàn bộ menu + phím tắt thật của app
   (kể cả phím tắt tuỳ chỉnh, giao diện tiếng Việt); `menu_select` bấm đúng mục theo đường dẫn.
3. **Macro bàn phím** có tham số (vd. "đi tới thư mục ~/X" = ⇧⌘G → gõ → ⏎).
4. **Phần tử Accessibility** — `element_find` / `element_click` / `element_set_value`.
5. **Nhìn màn hình (VLM)** — chỉ khi 1–4 không diễn đạt được bước đó.

## Thành phần
| File | Vai trò |
|---|---|
| `data/shortcuts/macos.yaml` | ~200 phím tắt, 17 nhóm (hệ thống, chuẩn, soạn thảo, Finder, Chrome/Edge/Brave/Arc, Safari, Gmail web, Mail, Notes, Preview, Terminal, VS Code/Cursor, Office/iWork, Excel, chat apps, Slack) + macro. Mỗi mục: phím, câu lệnh Việt/Anh, mô tả, mức rủi ro, điều kiện (`text_focus`, `no_text_focus`, `url`), cách xác minh, menu dự phòng. |
| `automation/a11y_macos.py` | 3 action L2 mới: `ui_snapshot` (app/cửa sổ/ô đang focus/giá trị/vùng chọn/URL — vài ms, không chụp ảnh), `menu_list`, `menu_select` (menu Apple bị chặn; menu nguy hiểm luôn hiện hộp thoại xác nhận). |
| `core/keyboard_first.py` | Tải kho, khớp lệnh **toàn câu** (không cướp lệnh dài hơn), nhận biết app đang mở hoặc app được nhắc tên ("… trong chrome" → mở Chrome trước), macro có tham số giữ nguyên chữ hoa/dấu, cổng xác nhận rủi ro, xác minh bằng `ui_snapshot` trước/sau, tự chuyển sang menu khi phím tắt không có tác dụng. |
| `utils/smart_actions.py` | Đường nhanh: lệnh khớp kho được chạy ngay, không gọi model. Lệnh bị người dùng từ chối/quá rủi ro dừng hẳn, không chuyển xuống tầng AI. |
| `planner/react_reasoner.py` | Tool `shortcut` (danh sách phím tắt của app đang mở), `menu_list`, `menu_select`, `ui_snapshot` cho agent; system prompt yêu cầu ưu tiên bàn phím/menu. |
| `core/agent_loop.py` | **Lazy vision**: sau thao tác bàn phím / Accessibility, bước tiếp theo dùng `ui_snapshot` thay vì chạy VLM (`models.lazy_vision`). Toạ độ VLM được đổi sang toạ độ màn hình trước khi đưa cho model — cùng hệ với Accessibility. |
| Workflow | Node mới `shortcut` / `menu` / `macro` (không cần nhìn màn hình). |

## An toàn
- Mức rủi ro `high` (gửi email/tin nhắn, chuyển vào Thùng rác, khoá màn hình…) → hỏi xác nhận;
  `critical` (dọn sạch Thùng rác) → không chạy tự động.
- Phím tắt một phím (Gmail `c`, `r`…) chỉ dùng khi KHÔNG đang gõ chữ (tránh gõ nhầm ký tự).
- Mọi thao tác vẫn đi qua IPC có HMAC + schema; L1 không tự phát sự kiện bàn phím.

## Mở rộng
Thêm phím tắt: sửa `data/shortcuts/macos.yaml`, chạy `./venv/bin/python -m unittest tests.test_keyboard_first`.
Phím tắt riêng của từng app không cần khai báo: `menu_list` đọc trực tiếp từ menu của app.
