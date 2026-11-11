# Luồng bài tổng quan tường thuật (giữ riêng để tương thích)

Hướng A/B hiện tại được mô tả tại `README_WORKFLOWS.md`; hướng A nghiên cứu dữ liệu theo đề tài,
không phải luồng tổng quan trong tài liệu này. Giao diện vẫn cho chọn **Bài tổng quan tài liệu** riêng
ngay khi tạo project. Loại bài được lưu trong PostgreSQL và trả về từ `/plan/{project_id}`.
API cũ không gửi `paper_type` vẫn tạo `original_research` để giữ tương thích với project cũ.
Với project đang có, dùng ô **Loại bản thảo** ở thanh bên để đổi luồng; tài liệu và các phiên bản
đã lưu được giữ lại. API tương ứng: `PATCH /plan/{project_id}/paper-type` với `paper_type`.

## Luồng tổng quan hoàn chỉnh

1. Lập kế hoạch từ chủ đề và phạm vi.
2. Tìm tài liệu, lọc liên quan, lưu metadata và nhật ký truy vấn/phạm vi/số bài được chọn.
3. Thử đọc toàn văn truy cập được; dùng tóm tắt khi không có toàn văn. Trích xuất phương pháp,
   đối tượng, phát hiện và hạn chế; tổng hợp chủ đề và khoảng trống tiềm năng.
4. Tạo bản thảo tổng quan riêng gồm Tóm tắt, Mở đầu, Phương pháp tổng quan,
   Tổng hợp bằng chứng theo chủ đề, Thảo luận, Khoảng trống/hướng nghiên cứu và Kết luận.
5. Tự kiểm chứng bằng mã và đối chiếu nội dung qua LLM. Khi có lỗi, hệ thống tự sửa
   đúng các đoạn bị đánh dấu rồi kiểm chứng lại, tối đa ba vòng; không đổi snapshot nguồn
   hoặc giảm tiêu chuẩn kiểm chứng. Mỗi thay đổi được lưu để truy vết; bản sửa có phiên bản mới.
6. Chỉnh sửa tay là tùy chọn qua nút **Chỉnh sửa nội dung** sau khi xuất. Có thể xem nguồn,
   sửa đoạn viết và thêm/bỏ liên kết bằng chứng; phiên bản sửa phải kiểm chứng lại.
7. Xuất PDF theo bố cục bài báo hoặc Word DOCX có thể chỉnh sửa. Tài liệu tham khảo được
   tạo từ các nguồn thực tế, mã trích dẫn được đánh số lại nhất quán.

Nhánh này **không gọi Dataset Finder hoặc Statistical Analysis**, không cần chọn một gap
để sinh giả thuyết thực nghiệm, không phụ thuộc vào bảng Introduction/Discussion của nhánh gốc.
Không tạo số khảo sát hay kết quả thống kê mới.

## Kiểm chứng và giới hạn khoa học

- Mỗi đoạn nội dung có danh sách nguồn; citation trong đoạn phải khớp các nguồn được gắn.
- Trích đoạn nguyên văn phải tồn tại trong snapshot của đúng nguồn và đúng bài báo.
- Số liệu trong đoạn viết phải xuất hiện trong trích đoạn hỗ trợ (kiểm tra bảo thủ;
  những biến đổi cách biểu diễn/làm tròn có thể cần sửa hoặc rà soát).
- LLM kiểm định từng đoạn trong lô nhỏ, phải trả đủ quyết định; thiếu/trùng quyết định là lỗi.
- Nhận định không được bằng chứng hỗ trợ bị đánh dấu và chặn xuất.
  Trước khi dừng, hệ thống tự sửa trích dẫn/quote và viết lại nhận định theo nguồn.
  Nếu sau ba vòng vẫn chưa đạt, vẫn chặn xuất và báo cần thêm nguồn hoặc điều chỉnh phạm vi,
  không mặc định bắt người dùng chữa từng lỗi citation. Không lặp gọi LLM nếu phiên bản đã đạt.
- Sửa bản thảo tạo phiên bản mới có trạng thái `draft`; không tái sử dụng kiểm chứng cũ.
- `validated` nghĩa là đạt kiểm chứng tự động, **không phải** được chuyên gia thẩm định.
  Khớp câu trích và kiểm định LLM không bảo đảm mọi suy luận đúng; cần tác giả rà soát.
- Bài xuất là **tổng quan tường thuật có hỗ trợ tự động**, không tự nhận tổng quan hệ thống,
  PRISMA hay phân tích gộp. Chưa đánh giá nguy cơ sai lệch bằng công cụ chuyên ngành.
- Tối đa 40 bài đã phân tích, có nội dung nguồn đọc được, ưu tiên năm mới nhất; mỗi bài dùng
  tóm tắt tối đa 2.600 ký tự và tối đa hai trích đoạn toàn văn, mỗi đoạn 1.600 ký tự.
  Giới hạn được ghi trong phương pháp khi có cắt tập nguồn. Có đoạn toàn văn không đồng nghĩa
  với đọc đầy đủ toàn bộ công trình. Cần tăng cường sàng lọc và đánh giá chất lượng cho tổng quan hệ thống.
- Tập nguồn là tài liệu được lưu trong project; nhật ký mới nhất mô tả lần tìm kiếm gần nhất,
  không được xem là sơ đồ sàng lọc cho toàn bộ tài liệu tích lũy trong project.

## API

Tạo project: `POST /plan` với `paper_type="review"` hoặc `"original_research"`.
Các bước tìm/đọc/tổng hợp tài liệu vẫn dùng `/search`, `/analyze`, `/synthesize`.

| Endpoint | Chức năng |
|---|---|
| `POST /reviews/project/{project_id}` | Viết và lưu bản thảo từ nguồn của project tổng quan |
| `GET /reviews/project/{project_id}` | Liệt kê các phiên bản |
| `GET /reviews/{id}` | Lấy nội dung, snapshot bằng chứng và báo cáo kiểm chứng |
| `PUT /reviews/{id}` | Lưu phiên bản chỉnh sửa mới |
| `POST /reviews/{id}/validate` | Kiểm chứng cấu trúc, trích đoạn, số liệu và nội dung |
| `POST /reviews/{id}/export/pdf` | Xuất PDF khi kiểm chứng đạt |
| `POST /reviews/{id}/export/docx` | Xuất Word khi kiểm chứng đạt |

PUT nhận `title` và `sections`, ánh xạ mỗi key `abstract`, `introduction`, `synthesis`,
`discussion`, `gaps`, `conclusion` thành `{ "paragraphs": [...] }`. Mỗi paragraph có `text`
và `evidence: [{paper_id, source_id, quote}]`. Mục `methodology` do hệ thống dựng từ nhật ký,
không nhận nội dung tự khai. Snapshot nguồn không được thay bằng dữ liệu từ request sửa.
Hai endpoint export nhận các tùy chọn tác giả/từ khóa như `PaperExportOptions`; đổi tiêu đề
phải thông qua bản chỉnh sửa và kiểm chứng mới. File xuất tạm được xóa sau khi gửi.

## Cập nhật và kiểm thử

```bash
docker compose up -d --build
```

Schema được bổ sung khi khởi động; project cũ giữ nhánh nghiên cứu gốc. Tiến trình giao diện
cũ được chuyển chỉ số bước, giữ project và nút tải, không bắt chạy lại nghiên cứu.

```bash
python -m pytest -q tests/test_review_workflow.py
python -m tests.review_browser_smoke
```

Kiểm thử trình duyệt cần Chrome tại đường dẫn Windows trong script, dùng hồ sơ riêng và
API giả lập. Kiểm thử PostgreSQL thật trong `tests/test_review_integration.py` chỉ chạy khi
`DATABASE_URL` trỏ tới user `httm_review_test`, cổng `65433`; không dùng database người dùng.
