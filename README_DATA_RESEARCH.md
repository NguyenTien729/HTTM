# HTTM — Data Research module (MVP)

Module này bổ sung đúng luồng còn thiếu cho bài nghiên cứu:

1. **Discover** — người dùng chọn một Research Gap đã phát hiện; hệ thống mở rộng các cách diễn đạt gần nghĩa của chính gap đó rồi tìm dataset công khai trên Hugging Face Hub.
2. **Inspect / human review** — lấy danh sách config/split, tên cột và 20 dòng mẫu để người dùng kiểm tra rồi tự chọn.
3. **Analyze after approval** — chỉ phân tích khi người dùng gửi `confirm_dataset=true`; tải Parquet qua Hugging Face Dataset Viewer, giới hạn tối đa 100 MB và 100.000 dòng mẫu.
4. **Statistics + writing draft** — sinh thống kê mô tả, dữ liệu thiếu/trùng, phân phối biến phân loại, tương quan Pearson, kiểm định nhóm (Welch t-test/Mann–Whitney hoặc ANOVA/Kruskal–Wallis/chi-square), OLS khi người dùng chỉ định biến, cùng bản nháp Methods và Results từ kết quả tính được.

> Đây là module API cho backend FastAPI hiện tại, chưa phải giao diện web riêng. Các endpoint xuất hiện trong Swagger `/docs` sau khi gắn router.

## Cài bản cập nhật vào repo HTTM

Bản cập nhật này thay đổi các file Data Research hiện có. Giải nén patch vào **thư mục gốc repo HTTM** và chọn ghi đè đúng các file có cùng đường dẫn:

- `app/data_research/service.py`
- `app/data_research/router.py`
- `app/data_research/planner_integration.py`
- `tests/test_data_research.py`
- `README_DATA_RESEARCH.md`

Không cần sửa `app/main.py` hoặc chạy `apply_patch.py` nếu router Data Research đã được gắn như trong bản repo này. Các dependency cần thiết đã nằm trong `requirements.txt`; chạy `pip install -r requirements.txt` nếu môi trường chưa cài đủ.

Để mở rộng từ khóa gần nghĩa, cấu hình `DEEPSEEK_API_KEY` trong `.env`. Nếu LLM không khả dụng, hệ thống vẫn thử các cụm từ dự phòng lấy từ mô tả và câu hỏi của gap đã chọn; khả năng mở rộng ngữ nghĩa khi đó sẽ hạn chế hơn.

Chạy ứng dụng và test:

```bash
uvicorn app.main:app --reload --port 8000
python -m pytest -q tests/test_data_research.py
```

Swagger: `http://127.0.0.1:8000/docs` (tag **Data Research**).

## Luồng dùng trong Swagger

### A. Chọn Research Gap rồi tìm dataset

Trước tiên, lấy danh sách gap của project:

`GET /synthesize/{project_id}/gaps`

Người dùng đọc danh sách và chọn đúng gap cần tìm dataset. Lấy `id` của gap được chọn, sau đó gọi:

`POST /data-research/discover`

```json
{
  "project_id": 42,
  "gap_id": 9,
  "limit": 10,
  "max_search_seconds": 60
}
```

- `project_id`: project chứa gap đã chọn.
- `gap_id`: ID của đúng gap được chọn trong danh sách `/synthesize/{project_id}/gaps`. Backend kiểm tra gap này có thuộc project hay không.
- `limit`: số dataset phù hợp cần tìm (1-20, mặc định 10).
- `max_search_seconds`: tổng thời gian tối đa cho cả mở rộng truy vấn và gọi Hugging Face (5-180 giây, mặc định 60).

**Không gửi `topic` vào endpoint này.** Hệ thống lấy `gap_type`, `description` và `proposed_research_question` từ bản ghi gap đã chọn. Nội dung topic tổng quát của Research Planner không được dùng làm input tìm kiếm.

Cách tìm: LLM tạo các cụm từ tiếng Anh gần nghĩa/tương đương, thuật ngữ chuyên ngành, cách gọi khác của tác vụ/nhãn dữ liệu và nhóm đối tượng; hệ thống tìm Hugging Face lần lượt theo từng cụm, loại ID trùng, xếp hạng theo mức khớp với metadata (tên, mô tả, tags, task categories). Khi LLM không khả dụng, hệ thống dùng các cụm từ lấy trực tiếp từ mô tả gap và một số từ thay thế xác định trước.

**Điều kiện dừng:** dừng ngay khi đủ `limit` dataset có metadata khớp; nếu chưa đủ thì tiếp tục mở rộng truy vấn và dừng khi chạm `max_search_seconds`. Trường `search.stop_reason` cho biết `enough_results`, `time_limit`, `query_exhausted` hoặc `source_rate_limited`. `query_exhausted` nghĩa là không tạo thêm được truy vấn mới, ví dụ LLM không khả dụng và các cụm dự phòng đã dùng hết.

Response có `gap` để xác nhận input đúng gap, `datasets` chứa `dataset_id`, mô tả, lượt tải, tags, license và URL; `search.queries_used` cho biết các truy vấn thực tế đã chạy. `gap_match_score` là điểm khớp từ metadata, không phải điểm semantic embedding và không thay thế bước kiểm tra thủ công.

### B. Kiểm tra dataset rồi mới chọn

`POST /data-research/inspect`

```json
{
  "dataset_id": "owner/dataset-name"
}
```

Thay `owner/dataset-name` bằng ID thật từ bước Discover. Phản hồi gồm `available_splits`, `columns`, `preview_rows` và checklist đánh giá. Sau khi xem mẫu, chọn chính xác `config` và `split` muốn sử dụng.

### C. Chạy thống kê sau khi xác nhận

`POST /data-research/analyze`

```json
{
  "dataset_id": "owner/dataset-name",
  "config": "default",
  "split": "train",
  "confirm_dataset": true,
  "max_rows": 50000,
  "target_column": "score",
  "group_column": "group",
  "predictor_columns": ["hours", "age"]
}
```

Các tên `config`, `split`, `score`, `group`, `hours`, `age` ở trên chỉ là ví dụ; phải thay bằng các giá trị/cột thực tế trong response của `/inspect`. Có thể bỏ `target_column`, `group_column` và `predictor_columns` nếu chỉ cần thống kê mô tả/tương quan.

## Lưu ý về Methods / Results

- Module không tự bịa kết quả; các số trong bản nháp được tạo từ DataFrame đã tải về.
- Khi split lớn hơn `max_rows`, module dùng **reservoir sampling với seed 42**, không lấy đơn thuần các dòng đầu tiên. Metadata trả về số dòng thực của split và số dòng được phân tích.
- Hiện giới hạn tải là 100 MB tổng, 32 MB/file và tối đa 40 parquet shards. Dataset không có Parquet công khai có thể không phân tích được bằng luồng này.
- Kiểm định là phân tích khám phá; p-value chưa hiệu chỉnh đa kiểm định và không chứng minh quan hệ nhân quả. Người làm nghiên cứu phải kiểm tra giả định, đại diện mẫu, giấy phép và các vấn đề đạo đức trước khi đưa vào bài báo.
- Dataset được tìm trên Hugging Face Hub; không tự động tìm mọi nguồn như Kaggle/UCI vì các nguồn đó có thể yêu cầu đăng nhập hoặc điều khoản sử dụng riêng.
- Tìm gần nghĩa hiện thực hiện bằng mở rộng truy vấn LLM + tìm kiếm văn bản của Hugging Face + chấm điểm metadata, chưa phải vector search/embedding trên toàn bộ nội dung dataset. Vì vậy phải kiểm tra `/inspect` trước khi xác nhận.
