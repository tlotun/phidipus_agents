# Bảo mật / Security policy

## Báo lỗ hổng
Vui lòng **không** mở issue công khai. Dùng GitHub → tab **Security** → **Report a vulnerability**
(Private vulnerability reporting) của repo này, hoặc liên hệ phidipuscom@gmail.com.
Chúng tôi phản hồi trong 72 giờ và công bố bản sửa trước khi tiết lộ chi tiết.

## Phạm vi đáng quan tâm
- Admin panel (`127.0.0.1:8912`): xác thực, chống CSRF / DNS-rebinding / chiếm WebSocket.
- Kênh IPC L1↔L2 (`/tmp/phidipus_ipc.sock`, HMAC + schema).
- Skill Forge sandbox (AST gate, subprocess + RLIMIT, bridge công cụ).
- Lệnh terminal (whitelist), bộ lọc prompt-injection, bộ lọc bí mật của Memory Agent.
- Telegram bot (xác thực admin, xác nhận thao tác nguy hiểm).

## Thiết kế an toàn mặc định
- Mọi dịch vụ chỉ lắng nghe trên localhost; không có tài khoản mặc định; đăng nhập từ xa cần
  password hash (scrypt) + domain được khai báo.
- Không telemetry. Giấy phép thương mại kiểm tra offline.
- Bộ nhớ dài hạn lưu cục bộ (SQLite, quyền 600), không gửi lên LLM đám mây trừ khi bạn bật
  `memory_agent.share_with_cloud`.

## Phiên bản được hỗ trợ
Chỉ bản mới nhất (4.3.x) nhận bản vá bảo mật.
