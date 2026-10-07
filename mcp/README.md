# MCP Integration — Model Context Protocol
## Kết nối Phidipus với hệ sinh thái tool bên ngoài

---

## Phidipus hỗ trợ MCP 2 chiều:

### 1. MCP Client — Phidipus kết nối TỚI tool bên ngoài
Phidipus có thể gọi tools từ các MCP server (Slack, GitHub, Google Drive, v.v.)

```python
# Trong workflow hoặc code
from mcp.mcp_registry import get_registry

reg = get_registry()
await reg.add_server("slack", url="https://mcp.slack.com/sse")
result = await reg.call("slack__send_message", {"channel": "#general", "text": "Hello"})
```

### 2. MCP Server — Tool bên ngoài gọi Phidipus Chrome
Chạy MCP server để Claude Desktop, n8n, hoặc browser-use gọi Chrome skills:

```bash
python mcp/mcp_chrome_server.py --port 3100
```

Claude Desktop config (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "phidipus-chrome": {
      "type": "url",
      "url": "http://localhost:3100/sse"
    }
  }
}
```

---

## API Endpoints (Admin Panel)

| Endpoint | Method | Chức năng |
|---|---|---|
| `/api/v2/mcp/status` | GET | Trạng thái tất cả MCP servers |
| `/api/v2/mcp/well-known` | GET | Danh sách MCP servers phổ biến |
| `/api/v2/mcp/connect` | POST | Kết nối MCP server mới |
| `/api/v2/mcp/disconnect` | POST | Ngắt kết nối MCP server |
| `/api/v2/mcp/tools` | GET | Danh sách tất cả tools |
| `/api/v2/mcp/call` | POST | Gọi 1 tool |

## Well-Known Servers

| Server | Icon | Cần | Mô tả |
|---|---|---|---|
| slack | 💬 | SLACK_BOT_TOKEN | Gửi tin, quản lý channel |
| github | 🐙 | GITHUB_TOKEN | Issues, PRs, repos |
| filesystem | 📁 | — | Đọc/ghi file |
| brave-search | 🔍 | BRAVE_API_KEY | Tìm kiếm web |
| google-drive | 📄 | OAuth | Google Drive |
| desktop-commander | 🖥️ | — | Điều khiển desktop |
| memory | 🧠 | — | Bộ nhớ dài hạn |

## Chrome Tools exposed via MCP

13 tools Chrome skills:

| Tool | Mô tả |
|---|---|
| `navigate` | Mở URL |
| `search_google` | Tìm Google → kết quả |
| `get_page_text` | Lấy text trang hiện tại |
| `click_element` | Click element |
| `fill_form` | Điền form |
| `extract_links` | Lấy danh sách link |
| `take_screenshot` | Chụp màn hình |
| `scroll_page` | Cuộn trang |
| `open_new_tab` | Mở tab mới |
| `run_javascript` | Chạy JS |
| `read_table` | Đọc bảng HTML |
| `get_page_title` | Lấy title trang |
| `get_current_url` | Lấy URL hiện tại |
