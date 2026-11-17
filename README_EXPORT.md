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
Phần dựng PDF không tạo thêm kết quả nghiên cứu. Khi tiêu đề chủ đề cũ còn tiếng Anh,
API dịch cả tiêu đề sang tiếng Việt (không thay nhan đề tài liệu tham khảo). Chú giải
biến được đề xuất từ mô tả/bảng mã nguồn; chỉ giữ liên kết biến có trích đoạn khớp nguồn.
Nếu thiếu bảng mã, không tự bịa nội dung câu hỏi từ các mã như PEVAI1.

Bảng thống kê dùng nhãn tiếng Việt, số làm tròn và dấu phẩy thập phân, giải thích cách
đọc trung bình/độ lệch chuẩn/trung vị. Nhóm mã phân loại được trình bày bằng tần số;
không coi mã giới tính/năm học là điểm liên tục khi có thông tin mã hóa từ nguồn.
Biểu đồ vector gồm trung bình từng chỉ báo theo nhóm nguồn, phân bố giá trị khi có
dữ liệu biểu đồ, cơ cấu mẫu và tương quan đã tính. Không tự tạo điểm tổng hợp thang đo.
Danh sách tương quan mạnh nhất không đủ để kết luận một cặp vắng mặt là không liên hệ.

Các lần phân tích mới lưu đủ thống kê cột, ma trận tương quan và dữ liệu biểu đồ vào
snapshot Discussion. Bản cũ chỉ có một phần thống kê sẽ được ghi rõ giới hạn; muốn có
đủ mọi biến cần chạy lại phân tích rồi lưu Discussion mới trước khi xuất.

`artifacts/export-preview/bai-bao-minh-hoa.pdf` dùng dữ liệu thử để minh họa bố cục,
không phải kết quả nghiên cứu thực tế. Endpoint proposal giữ bố cục riêng.
