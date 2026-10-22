# Bụi Records

Website bán đĩa than cho cửa hàng Bụi Records (Hà Nội): khách xem đĩa, đặt hàng, giữ chỗ đặt trước và theo dõi đơn; cửa hàng quản lý sản phẩm, tồn kho, đơn hàng và sự cố sau bán tại trang `/admin`.

- **Backend:** FastAPI + SQLite (`backend/`)
- **Frontend:** HTML/CSS/JS thuần, do chính backend phục vụ (`frontend/`)
- **Discogs API:** cung cấp thông tin đĩa (ảnh bìa, nghệ sĩ, tracklist...). Giá bán và tồn kho là dữ liệu riêng của shop, lưu trong SQLite.
- **Giỏ hàng** nằm trong cookie phiên đã ký, không lưu vào DB.

## Cài đặt

Cần Python 3.10 trở lên.

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Tạo file `.env` ở thư mục gốc dự án (cạnh `backend/` và `frontend/`):

```text
DISCOGS_TOKEN=token_ca_nhan_cua_ban          # hoặc DISCOGS_CONSUMER_KEY + DISCOGS_CONSUMER_SECRET
DISCOGS_USER_AGENT=BuiRecordsApp/2.0 (+https://your-site.example)
SECRET_KEY=chuoi_ngau_nhien_dai               # ký cookie phiên
ADMIN_TOKEN=ma_dang_nhap_trang_admin
```

Tùy chọn: `COOKIE_SECURE=1` khi chạy qua HTTPS, `ALLOWED_ORIGIN_REGEX` nếu frontend nằm ở domain khác, `DEV_RELOAD=1` để tự tải lại khi sửa code.

## Chạy

```bash
cd backend
python app.py
```

- Cửa hàng: http://127.0.0.1:5000
- Trang quản trị: http://127.0.0.1:5000/admin (đăng nhập bằng `ADMIN_TOKEN`)

Database `backend/bui_records.db` được tạo tự động ở lần chạy đầu.

## Thêm sản phẩm

Cách dễ nhất là vào `/admin` → **Sản phẩm** → **Thêm sản phẩm**, rồi dán mã hoặc link release Discogs. Ngoài ra có thể dùng dòng lệnh:

```bash
python database.py set-price 249504 1010000      # đặt giá cho release Discogs 249504
python database.py set-stock 249504 5            # số bản trong kho (none = không đếm)
python database.py import-prices prices.csv      # nhập hàng loạt từ CSV
python database.py                               # xem tất cả lệnh
```
