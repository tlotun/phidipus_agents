## Thay đổi / What changed
<!-- Mô tả ngắn; liên kết issue liên quan (ví dụ "Closes" + số issue). -->

## Kiểm tra / How was it tested?
<!-- Lệnh đã chạy, macOS + ứng dụng đã thử; ảnh chụp nếu đổi giao diện. -->

## Checklist
- [ ] `./venv/bin/python -m unittest discover -s tests -t .` đạt
- [ ] `./venv/bin/python tools/check_secrets.py` sạch (khoá giả trong test được ghép chuỗi lúc chạy)
- [ ] File mới có dòng SPDX + Copyright (xem CONTRIBUTING.md)
- [ ] Thao tác không hoàn tác được có `risk: high` hoặc hỏi xác nhận
- [ ] Đã cập nhật tài liệu / CHANGELOG.md nếu cần

## CLA
<!-- Bắt buộc với người đóng góp lần đầu: đọc CLA.md rồi đánh dấu ô dưới. -->
- [ ] I have read and agree to the Phidipus Agents CLA (CLA.md).
