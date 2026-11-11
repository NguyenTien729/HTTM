# Hướng A và hướng B của HTTM

## Hướng A — nghiên cứu trực tiếp theo đề tài

Đây là lựa chọn mặc định khi tạo project trên giao diện.

**Đề tài → tìm tài liệu → đọc/trích xuất bằng chứng → tổng hợp theo đề tài → câu hỏi nghiên cứu
theo đề tài → viết phần mở đầu và tổng quan → tìm dataset theo đề tài → kiểm tra/chọn dataset
→ phân tích dữ liệu → Discussion & Conclusion từ kết quả thực tế → xuất PDF.**

- Không chạy phát hiện, duyệt hoặc chọn research gap.
- Không tạo gap giả để đáp ứng ràng buộc database. `research_questions.gap_id` được phép NULL.
- Câu hỏi được tạo trực tiếp từ `research_projects.topic`; tài liệu chỉ giúp làm rõ khái niệm
  và cách đo, không chuyển câu hỏi sang một đề tài khác. Không sinh giả thuyết mới từ gap.
- Biến được đề xuất trong câu hỏi là khái niệm nghiên cứu, chưa được coi là tên cột dataset.
- Dataset Finder lấy chủ đề project làm tiêu chí chính, kể cả khi trình duyệt còn ID gap cũ.
- Người dùng kiểm tra dataset, chọn biến thực tế và xác nhận trước khi tải/phân tích.
- Thống kê được tính từ dữ liệu; Discussion/Conclusion sử dụng snapshot phân tích thật.
- Dataset chỉ có metadata không đủ để hoàn tất phân tích thực nghiệm ở hướng A; hệ thống
  báo cần dataset có biến đo phù hợp. Không tự tạo dữ liệu hoặc kết quả để đi tiếp.
- Nếu không tìm được dataset phù hợp, không tự chuyển thành bài tổng quan hoặc thêm dataset
  lệch đề tài; bước chọn dữ liệu cho phép nhập ID nguồn Hugging Face phù hợp.

### API hướng A

```json
POST /plan
{
  "topic": "Đề tài của người dùng",
  "paper_type": "topic_research"
}
```

Sau các bước `/search/{project_id}`, `/analyze/{project_id}`, `/synthesize/{project_id}`:

- `POST /research-questions/project/{project_id}` sinh câu hỏi trực tiếp từ đề tài, trả `gap_id=null`.
- `POST /write/introduction/{rq_id}` và `POST /write/related-work/{rq_id}` chấp nhận câu hỏi không gap.
- `POST /data-research/discover` chỉ cần `project_id` (cùng tùy chọn limit/thời gian).
- `POST /data-research/analyze` nhận project, dataset/split, xác nhận và biến phân tích như trước.
- `POST /write/discussion/{rq_id}` nhận `analysis_result`; không tự thêm đánh giá giả thuyết khi câu hỏi theo đề tài.
- `POST /export/paper/{rq_id}` dựng bài từ các phần đã lưu, kể cả khi không có gap/giả thuyết.

API `/plan` không gửi `paper_type` vẫn giữ mặc định cũ `original_research` để tương thích;
giao diện mới luôn gửi loại luồng được chọn.

## Hướng B — nghiên cứu từ khoảng trống

Giữ luồng hiện có: đề tài → tài liệu → phát hiện/duyệt gap → câu hỏi và giả thuyết → chọn dữ liệu
→ phân tích → viết bài. Giá trị API là `original_research`.

Việc thiết kế thí nghiệm hoặc **tạo dataset** cho hướng B chưa được bổ sung trong đợt này.
Không tạo dữ liệu tổng hợp rồi trình bày như dữ liệu quan sát thực tế.

## Project đã tồn tại

Sau khi cập nhật, chọn **Hướng A — nghiên cứu theo đề tài** trong ô Loại bản thảo ở thanh bên.
Tài liệu và các bản thảo đã lưu được giữ; hệ thống quay lại bước tạo câu hỏi theo đề tài.
API chuyển tương ứng là `PATCH /plan/{project_id}/paper-type` với `paper_type="topic_research"`.

Luồng bài tổng quan tường thuật trước đây vẫn được giữ riêng để tương thích, xem
`README_REVIEW_WORKFLOW.md`; luồng đó không còn được gọi là hướng A.

```bash
docker compose up -d --build
```

Tải lại giao diện bằng Ctrl+F5. Kiểm thử chính: `tests/test_topic_workflow.py`,
`tests/test_topic_integration.py` (PostgreSQL cách ly), `python -m tests.review_browser_smoke`
(Chrome với API giả lập, kiểm tra cả A, tổng quan và tiến trình cũ).
