# HTTM — Data Research module (MVP)

Module này bổ sung đúng luồng còn thiếu cho bài nghiên cứu:

1. **Discover** — tìm dataset công khai liên quan đến chủ đề trên Hugging Face Hub.
2. **Inspect / human review** — lấy danh sách config/split, tên cột và 20 dòng mẫu để người dùng kiểm tra rồi tự chọn.
3. **Analyze after approval** — chỉ phân tích khi người dùng gửi `confirm_dataset=true`; tải Parquet qua Hugging Face Dataset Viewer, giới hạn tối đa 100 MB và 100.000 dòng mẫu.
4. **Statistics + writing draft** — sinh thống kê mô tả, dữ liệu thiếu/trùng, phân phối biến phân loại, tương quan Pearson, kiểm định nhóm (Welch t-test/Mann–Whitney hoặc ANOVA/Kruskal–Wallis/chi-square), OLS khi người dùng chỉ định biến, cùng bản nháp Methods và Results từ kết quả tính được.

> Đây là module API cho backend FastAPI hiện tại, chưa phải giao diện web riêng. Các endpoint xuất hiện trong Swagger `/docs` sau khi gắn router.

## Cài vào repo

1. Tải ZIP patch và giải nén **vào thư mục gốc repo HTTM** (cùng cấp với `app/`, `tests/`, `requirements.txt`). Khi hệ điều hành hỏi có ghi đè `app/` hay không, chọn gộp thư mục; patch chỉ thêm `app/data_research/` và một test.
2. Từ thư mục gốc repo, chạy:

   ```bash
   python apply_patch.py
   pip install -r requirements.txt
   uvicorn app.main:app --reload --port 8000
   ```

3. Mở `http://127.0.0.1:8000/docs`, tìm tag **Data Research**.
4. Chạy test module:

   ```bash
   python -m pytest -q tests/test_data_research.py
   ```

Script `apply_patch.py` sẽ tự gắn router vào `app/main.py` nếu file có cấu trúc `app = FastAPI(...)`. Nếu cấu trúc thực tế khác, script sẽ dừng an toàn và không đoán; khi đó thêm thủ công dòng sau sau khi khởi tạo biến `app`:

```python
from app.data_research.router import router as data_research_router
app.include_router(data_research_router)
```

## Luồng dùng trong Swagger

### A. Tìm dataset

`POST /data-research/discover`

```json
{
  "topic": "Tác động của AI đến năng suất sinh viên đại học",
  "limit": 10
}
```

Kết quả chứa `dataset_id`, mô tả, lượt tải, tags, license nếu có và liên kết nguồn. Điểm khớp chủ đề chỉ là gợi ý từ metadata, không đảm bảo nội dung phù hợp.

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
