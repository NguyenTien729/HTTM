# HTTM

HTTM là ứng dụng FastAPI hỗ trợ tìm kiếm tài liệu, tổng hợp bằng chứng, nghiên cứu dữ liệu và soạn thảo bài báo khoa học.

## Chạy ứng dụng

Cần Python 3.12+, PostgreSQL và cấu hình kết nối trong `.env`.

```powershell
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
```

Mở giao diện tại <http://127.0.0.1:8000/> hoặc API docs tại <http://127.0.0.1:8000/docs>.

Có thể chạy PostgreSQL bằng `docker compose up -d db`. Lệnh `docker compose up -d --build` chạy cả ứng dụng và database.

## Luồng nghiên cứu tự động

Giao diện tại `/ui/autonomous.html` hướng dẫn từ đề tài, tìm và kiểm tra nguồn, duyệt phương án, phân tích dữ liệu đến bản thảo. Nếu thiếu dữ liệu hoặc phương pháp chưa phù hợp, HTTM dừng phần phân tích thực nghiệm và tạo riêng bản đề xuất có blocker; không tự tạo kết quả thực nghiệm.

## Tài liệu

- [Quy trình nghiên cứu tự động](README_AUTONOMOUS.md)
- [Quy trình thao tác và các hướng nghiên cứu](README_WORKFLOWS.md)
- [Tìm và phân tích dataset](README_DATA_RESEARCH.md)
- [Bài tổng quan tài liệu](README_REVIEW_WORKFLOW.md)
- [Xuất bài báo PDF](README_EXPORT.md)

## Cấu hình

Đặt `DATABASE_URL` và khóa API của nhà cung cấp LLM trong `.env`. Không commit `.env` hoặc dữ liệu nghiên cứu riêng tư.
