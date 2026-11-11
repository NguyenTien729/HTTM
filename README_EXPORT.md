# Xuất bài báo PDF

`POST /export/paper/{research_question_id}` xuất bài báo A4 một cột, font serif
Tinos hỗ trợ tiếng Việt, tiêu đề in hoa, tóm tắt in nghiêng, đề mục đánh số,
bảng lặp tiêu đề khi sang trang và tài liệu tham khảo có thụt dòng treo.
Font được đóng gói trong `app/assets/fonts`; giấy phép đi kèm `Tinos-OFL.txt`.
Không cần cài font trên máy hoặc trong Docker.

Body là tùy chọn; request cũ không có body vẫn hoạt động. Ví dụ cấu hình:

```json
{
  "title": "Tên bài báo nghiên cứu",
  "authors": [
    {
      "name": "Tên tác giả",
      "affiliation": "Đơn vị công tác",
      "email": "author@example.org",
      "corresponding": true
    }
  ],
  "keywords": ["từ khóa thứ nhất", "từ khóa thứ hai"],
  "include_author_notes": false
}
```

Giao diện có các trường tương ứng ở bước **Xuất bài báo PDF**. Mỗi dòng tác giả
có dạng `Họ tên | Đơn vị | Email | *`; dấu `*` đánh dấu tác giả liên hệ.
Nếu không nhập, tiêu đề lấy từ đề tài; không tự tạo tên tác giả hay đơn vị.
Không thêm tên tạp chí, logo, ISSN hoặc ngày duyệt đăng từ tài liệu mẫu.

Trang ghi chú nội bộ chỉ xuất khi chọn `include_author_notes=true`. Các cảnh báo
về citation/số liệu vẫn được giữ ở bước viết Discussion và trong dữ liệu đã lưu.
Export không gọi LLM hoặc tạo thêm kết quả nghiên cứu.

`artifacts/export-preview/bai-bao-minh-hoa.pdf` dùng dữ liệu thử để minh họa bố cục,
không phải kết quả nghiên cứu thực tế. Endpoint proposal giữ bố cục riêng.
