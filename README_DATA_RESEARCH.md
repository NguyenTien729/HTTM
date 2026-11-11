# HTTM — Data Research module (MVP)

## Nguồn dữ liệu nghiên cứu ngoài Hugging Face

Discover tìm cả Hugging Face và Mendeley Data qua DataCite. Truy vấn Mendeley giới hạn
loại tài nguyên **Dataset**, tìm đồng thời theo khái niệm và theo địa bàn trong topic.
Tên/biến thể địa bàn do bộ lập truy vấn suy ra từ từng project, không cố định một chủ đề.
Kết quả cùng địa bàn được ưu tiên; ứng viên thiếu một số khái niệm được ghi rõ
`partial_topic_match` và `missing_topic_concepts`, không coi là đáp ứng đủ đề tài.
`scope_match=unverified` nghĩa là chưa xác nhận địa bàn. Khớp metadata không chứng minh
biến trong file đo được năng suất khách quan hoặc tác động nhân quả.

ID Mendeley có dạng `mendeley:9krwng5dm7:1`. Các endpoint inspect/analyze tiếp tục dùng
`dataset_id`, `config`, `split`; với Mendeley, config là ID file do inspect trả về,
split là số thứ tự sheet Excel từ 0 (CSV dùng 0). Chỉ nhận file CSV/XLSX công khai trong
thư mục gốc; mặc định ưu tiên file dữ liệu thay vì bảng mã/bảng kết quả. API inspect
nhận config/split để kiểm tra file hoặc sheet khác. Giao diện hiện xem file mặc định.
Giới hạn 32 MB/file, 100.000 dòng đọc, 128 MB kích thước giải nén Excel; không tải ZIP
tổng, không thực thi macro hoặc tải URL tùy ý. Cần dependency `openpyxl` khi đọc Excel.

`python -m tests.check_mendeley_live` là kiểm tra trực tiếp nguồn công khai (không gọi LLM),
bao gồm tìm ứng viên, đọc Excel và tính thống kê. Đây là ca tái hiện cụ thể, không phải
danh sách dataset được cố định trong mã tìm kiếm.

Module này bổ sung đúng luồng còn thiếu cho bài nghiên cứu:

1. **Discover** — hệ thống lấy chủ đề đã lưu của project, tạo truy vấn và nhóm khái niệm phù hợp để tìm dataset công khai trên Hugging Face Hub; research gap là ngữ cảnh bổ sung tùy chọn.
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

### A. Tìm dataset theo chủ đề project

`POST /data-research/discover`

```json
{
  "project_id": 42,
  "gap_id": 9,
  "limit": 10,
  "max_search_seconds": 60
}
```

- `project_id` bắt buộc: backend lấy chủ đề đã lưu trong Research Planner.
- `gap_id` tùy chọn: thêm yêu cầu từ gap thuộc project, không thay thế chủ đề chính.
- Không cần nhập lại topic; request không nhận topic tự nhập.
- LLM tạo truy vấn và 2–4 nhóm khái niệm cốt lõi theo chủ đề của từng project.
  Mỗi nhóm gồm từ đồng nghĩa tiếng Việt không dấu và tiếng Anh; không có bộ lọc
  ngành cố định. Metadata ứng viên phải có bằng chứng khớp các nhóm này.
- Nếu LLM không khả dụng, chỉ đối chiếu trực tiếp từ khóa từ chủ đề project;
  khả năng khớp metadata tiếng Anh với topic tiếng Việt sẽ hạn chế hơn.
- Không giữ kết quả lệch chủ đề để đủ số lượng. Điểm khớp không xác nhận chất lượng,
  tính đại diện, địa bàn hoặc khả năng kiểm định nhân quả; vẫn cần bước inspect.
- Response ghi `input_source=project_topic`, `project_topic`, `gap`,
  `search.topic_concept_groups`, `search.queries_used` và số kết quả bị loại.
- Tìm trên Hugging Face và Mendeley Data/DataCite; không có kết quả không chứng minh dữ liệu không tồn tại.

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

### Phân tích theo vai trò biến

Dataset ảnh/audio được phân tích qua cột scalar và metadata lồng trong struct
(ví dụ `image.path`, `annotation.label`, `image_width`); các nhánh binary và list
không được tải. Tên chứa `image` hay `mask` không còn khiến cột số/chuỗi bị loại.
Nếu không có scalar, kết quả `analysis_mode=structure_only` chỉ báo schema/số
bản ghi từ metadata Parquet, không báo thống kê thiếu/trùng hoặc kiểm định nội dung.
Đây không phải phân tích thị giác máy tính hay xử lý tín hiệu audio.

Giao diện xác nhận dataset cho phép chọn vai trò từng cột và tối đa 20 biến
tương quan. API `/analyze` nhận thêm các trường tùy chọn:

```json
{
  "project_id": 42,
  "gap_id": 9,
  "column_roles": {
    "user_id": "identifier",
    "label": "categorical",
    "score": "continuous",
    "text": "text",
    "created_at": "datetime"
  },
  "correlation_columns": ["score", "hours"]
}
```

Đây là các trường bổ sung vào request Analyze ở trên; chỉ gửi tên cột thực tế.
`gap_id` yêu cầu `project_id` và được kiểm tra quyền sở hữu trước khi tải dữ liệu.
Vai trò tự nhận diện là gợi ý; nhãn số ngoài các tên thông dụng cần được xác nhận
là phân loại. Predictor phải là biến liên tục, không trùng nhau hoặc trùng target;
biến nhóm phải là phân loại. Mã định danh không được đưa vào tương quan mặc định.

Kết quả bổ sung `column_roles`, `quality_report`, `analysis_plan`,
`correlation_spearman`, `correlation_pair_counts` và `charts`. Giao diện hiển thị
histogram, phân bố nhãn, heatmap và boxplot theo nhóm (râu biểu diễn min/max).
Chuỗi rỗng và giá trị vô hạn được xem là thiếu; không tự xóa dòng trùng/ngoại lệ.
Các chỉ số chất lượng chỉ mô tả mẫu phân tích, không ước lượng số lỗi toàn split.

Phân tích hai nhóm có chênh lệch trung bình và CI 95% theo Welch; kiểm định nhóm
có p-value hiệu chỉnh Benjamini–Yekutieli trong họ các kiểm định nhóm được trả về.
Kruskal–Wallis là kiểm định chính cho từ ba nhóm; ANOVA thường là bổ sung và cần
kiểm tra giả định. Bảng 2×2 có tần số kỳ vọng nhỏ được bổ sung Fisher exact.
Hồi quy có sai số chuẩn/CI HC3 bên cạnh kết quả OLS thông thường; p-value OLS
gốc không phải p-value HC3. Bản nháp chỉ tuyên bố ước lượng mô hình khi chạy thành công.

Lấy mẫu reservoir thực hiện đúng `max_rows` (100–100.000), seed 42. Vẫn phải
duyệt toàn bộ các dòng của những cột bảng để tạo mẫu; tác vụ sẽ dừng nếu vượt
ngân sách truyền dữ liệu. Không tự xác nhận dataset đáp ứng gap: `analysis_plan`
giữ câu hỏi và checklist để người nghiên cứu đánh giá độ phù hợp.

- Module không tự bịa kết quả; các số trong bản nháp được tạo từ DataFrame đã tải về.
- Khi split lớn hơn `max_rows`, module dùng **reservoir sampling với seed 42**, không lấy đơn thuần các dòng đầu tiên. Metadata trả về số dòng thực của split và số dòng được phân tích.
- Hiện giới hạn tải là 100 MB truyền dữ liệu tổng và tối đa 40 parquet shards (đọc theo HTTP Range, mỗi vùng đọc tối đa 8 MB). Dataset không có Parquet công khai có thể không phân tích được bằng luồng này.
- Kiểm định là phân tích khám phá; p-value gốc trong bản nháp chưa hiệu chỉnh; bảng kiểm định nhóm có p-value hiệu chỉnh BY và không chứng minh quan hệ nhân quả. Người làm nghiên cứu phải kiểm tra giả định, đại diện mẫu, giấy phép và các vấn đề đạo đức trước khi đưa vào bài báo.
- Dataset được tìm trên Hugging Face và Mendeley Data; các kho khác như Kaggle/UCI chưa được tích hợp.
- Tìm gần nghĩa hiện thực hiện bằng mở rộng truy vấn LLM + tìm kiếm văn bản của Hugging Face + chấm điểm metadata, chưa phải vector search/embedding trên toàn bộ nội dung dataset. Vì vậy phải kiểm tra `/inspect` trước khi xác nhận.
