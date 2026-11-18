# HTTM — Autonomous Scientific Research & Writing

Luồng tự động được tích hợp vào FastAPI hiện có. Mở `/` hoặc
`/ui/autonomous.html`. Giao diện thao tác từng bước vẫn ở `/ui/index.html`.

## Nhánh nghiên cứu tài liệu (mặc định)

Chọn **Nghiên cứu tài liệu → bài tổng quan** khi tạo run. Project có sẵn phải
có loại `review`; các run thực nghiệm cũ vẫn giữ nguyên loại và lịch sử.

Luồng: tìm tài liệu → đọc và trích xuất → tổng hợp chủ đề → duyệt phạm vi →
viết từng phần → kiểm chứng và tự sửa (tối đa ba vòng) → duyệt bản cuối.
Không yêu cầu dataset hay chạy hồi quy trong nhánh này.

Tập nguồn, trích đoạn và nhật ký tìm kiếm được lưu vào hồ sơ run trước khi duyệt
phạm vi. Mỗi đoạn viết gắn mã tài liệu, mã nguồn và trích đoạn nguyên văn.
Kiểm chứng kiểm tra trích dẫn, số liệu và mức hỗ trợ của nguồn; nếu còn lỗi sau
tự sửa, người dùng có thể yêu cầu chỉnh sửa bản thảo. PDF và DOCX chỉ xuất sau
kiểm chứng và phê duyệt; Markdown và hồ sơ bằng chứng JSON có thể tải trước.

Đầu ra là **bài tổng quan tường thuật**, không tự nhận là tổng quan hệ thống,
PRISMA hay nghiên cứu thực nghiệm mới. Kiểm chứng nội dung bằng LLM vẫn cần
người nghiên cứu rà soát.

Ứng dụng trong Docker dùng mã đã đóng gói trong image. Cập nhật bằng
`docker compose up -d --build --no-deps app`, sau đó tải lại trang. Database
và volume nghiên cứu được giữ nguyên.

## Nhánh thực nghiệm

1. Người dùng nhập đề tài, tùy chọn địa bàn và tác giả, hoặc chọn project đã có.
2. Hệ thống tìm bài báo, đọc PDF mở khi có, dùng abstract khi không đọc được PDF,
   rồi tổng hợp bằng chứng bằng các module HTTM hiện có.
3. AI đề xuất câu hỏi, khoảng trống dự kiến, giả thuyết và kế hoạch dữ liệu:
   biến cần đo, đơn vị, độ phân giải không gian/thời gian, khóa ghép.
4. **Dừng để người dùng duyệt câu hỏi và yêu cầu dữ liệu.**
5. Tự tìm dữ liệu riêng cho từng biến từ Hugging Face và Mendeley Data/DataCite;
   kiểm tra schema, tải dữ liệu bảng có giới hạn và lưu snapshot Parquet.
   Người dùng không cần chọn dataset ban đầu.
6. AI đề xuất nguồn, ánh xạ cột, chuyển đơn vị, khóa kết hợp và phương pháp.
   Chương trình kiểm tra cột có thật, đủ biến, đơn vị đích, tính duy nhất của khóa,
   số dòng khớp, đủ quan sát và điều kiện hạng ma trận cho OLS.
7. **Dừng để duyệt toàn bộ thiết kế, nguồn và cách xử lý dữ liệu.** Không cho duyệt
   thiết kế thiếu biến hoặc phương pháp chưa được hỗ trợ.
8. Chạy phân tích thật trên snapshot đã duyệt; tạo thống kê, biểu đồ và bản nháp
   Methods/Results. Viết Introduction, Related Work, Discussion/Conclusion,
   Title/Abstract/Keywords. Chỉ dùng tên tác giả do người dùng cung cấp.
9. Kiểm tra ID trích dẫn, số liệu Discussion/Abstract, fingerprint nguồn dữ liệu,
   và kiểm tra ngữ nghĩa bằng LLM đối với bản thảo, bằng chứng và kết quả.
10. **Dừng để duyệt bản thảo cuối.** Các lỗi kiểm chứng mức `error` chặn duyệt.
    Sau khi duyệt có thể tải PDF. Markdown bản nháp và hồ sơ JSON có thể tải trước đó.

Nếu không có dữ liệu phù hợp, hệ thống báo thiếu dữ liệu tại bước thiết kế. Người dùng
có thể yêu cầu tìm lại, đề xuất phương pháp khác hoặc quay về sửa câu hỏi. Thay đổi câu
hỏi vô hiệu hóa toàn bộ dữ liệu, thiết kế, phân tích và bản thảo phía sau; thay đổi
phương pháp vô hiệu hóa kết quả và bài viết; sửa văn bản giữ lại phân tích đã duyệt.
Các phiên bản trước vẫn được lưu trong hồ sơ kiểm toán.

## Nhận dữ liệu của nhóm nghiên cứu (tìm **hoặc nhận** dữ liệu)

Ngoài việc tự tìm dữ liệu công khai, nhánh thực nghiệm nhận file do nhóm tự có
(khảo sát, số liệu xin từ đối tác, bảng tải từ cổng thống kê…). Thẻ **Dữ liệu của nhóm
nghiên cứu** xuất hiện trong giao diện khi tiến trình đang chờ duyệt câu hỏi, chờ duyệt
thiết kế, bị lỗi hoặc bị gián đoạn.

- Định dạng: CSV, TSV, XLSX (chọn trang tính, dòng tiêu đề), Parquet; tối đa 20 MB,
  50.000 dòng (và không vượt giới hạn dòng của tiến trình), 300 cột, 5 file mỗi tiến trình.
- Bắt buộc mô tả nguồn gốc, cách thu thập, địa bàn, thời gian, đơn vị; giấy phép tùy chọn.
- Tải ở bước duyệt câu hỏi: file được dùng ở bước thu thập. Có thể chọn **chỉ dùng dữ liệu
  của nhóm** để bỏ qua tìm kiếm công khai. Tải hoặc gỡ file sau bước thu thập: hệ thống chạy
  lại bước thiết kế để kiểm tra lại cột, đơn vị, khóa ghép trước khi cho phép phân tích.
- File tải lên được giữ qua mọi lần "yêu cầu điều chỉnh"; snapshot Parquet có SHA-256 như
  nguồn công khai và được kiểm tra lại ở bước kiểm chứng.

Làm sạch tất định (`app/autonomous/uploads.py`), mọi bước được ghi vào nhật ký và tự đưa
vào phần Methods:

1. Đọc mã hóa UTF-8 hoặc Windows-1258, tự nhận dấu phân cách `, ; tab |`.
2. Chuẩn hóa tên cột (khoảng trắng, Unicode NFC, cột không tên, tên trùng).
3. Cắt khoảng trắng, chuẩn hóa NFC cho ô văn bản; ô trống và ký hiệu thiếu (`NA`, `N/A`,
   `-`, `..`, lỗi Excel như `#N/A`) thành giá trị thiếu, có đếm theo từng cột.
4. Bỏ dòng và cột hoàn toàn trống.
5. Chuyển cột văn bản sang số **chỉ khi mọi giá trị** đọc được theo cùng một định dạng
   (`1.234,5` kiểu Việt Nam hoặc `1,234.5`). Cột mơ hồ (`1.234` có thể là 1,234 hoặc 1234)
   được giữ dạng văn bản, được cảnh báo, và thiết kế không được ép sang số; người dùng chọn
   định dạng số khi tải lại. Cột mã có số 0 đứng đầu (`01`) được giữ dạng mã để làm khóa ghép.
6. Đếm dòng trùng lặp hoàn toàn; chỉ loại khi người dùng chọn.

Không điền giá trị thiếu, không loại ngoại lai, không đổi đơn vị ở bước này; các quyết định
đó thuộc thiết kế phân tích đã duyệt. Hệ thống không xác minh cách thu thập, tính đại diện
hay quyền sử dụng của dữ liệu do nhóm cung cấp; giới hạn này được ghi vào phần Limitations.

```http
POST /autonomous/runs/{id}/sources/upload        (multipart/form-data)
  file, expected_version, description, license?, sheet?, header_row=1,
  number_format=auto|comma_decimal|dot_decimal, drop_duplicates=false, only_user_data=false
POST /autonomous/runs/{id}/sources/{source_key}/remove   {"expected_version": 9}
```

## Kiến trúc và tính bền vững

- `app/autonomous/models.py`: hồ sơ nghiên cứu, yêu cầu dữ liệu và thiết kế có cấu trúc.
- `state.py`: chuyển trạng thái và ba điểm duyệt; kiểm tra phiên bản và fingerprint.
- `store.py`: hàng đợi PostgreSQL, lịch sử phiên bản, lease và optimistic concurrency.
- `worker.py`: worker chạy nền cùng vòng đời ứng dụng, một bước tại một thời điểm.
- `data.py`: tìm/kiểm tra/tải nguồn, snapshot SHA-256, biến đổi và ghép dữ liệu.
- `pipeline.py`: sáu vai trò nghiên cứu, tái sử dụng các dịch vụ HTTM hiện có.
- `router.py`: API tiến trình, phê duyệt, tiếp tục, dừng và xuất bản thảo.
- `app/static/autonomous.*`: giao diện tạo nghiên cứu, xem hồ sơ và phê duyệt.

Schema PostgreSQL được thêm idempotent khi ứng dụng khởi động. Các bảng mới:
`autonomous_runs`, `autonomous_run_history`. Không xóa bảng hoặc dữ liệu của luồng cũ.
Mỗi project chỉ có một tiến trình tự động chưa kết thúc.

Worker nhận việc bằng `FOR UPDATE SKIP LOCKED`, có heartbeat mỗi 20 giây, lease 2 phút,
giới hạn mỗi bước 15 phút. Khi worker bị ngắt, tiến trình chuyển sang `interrupted`
sau khi lease hết hạn; người dùng chọn tiếp tục. Một worker cũ không thể ghi đè kết quả
sau khi người dùng đã dừng tiến trình hoặc phiên bản đã thay đổi. Các bước ghi qua module
cũ có thể sinh thêm bản nháp khi bị gián đoạn đúng lúc commit; bản thảo của run chỉ lấy
các artifact được lưu thành công. Không cam kết exactly-once với các API bên ngoài.

Snapshot ở `artifacts/autonomous/<run_id>/<revision>/`. Docker Compose gắn named volume
`research_artifacts` tại `/code/artifacts/autonomous`, cần giữ volume này cùng PostgreSQL
để có thể tiếp tục và tái lập. Phân tích lưu phiên bản Python/thư viện và SHA-256 mã nguồn.
PDF lấy nội dung từ snapshot run được duyệt, không lấy bản viết mới nhất của project khác.

## Cách tìm dữ liệu theo kiểu deep search

- Lập truy vấn riêng cho từng biến, gồm khái niệm, từ đồng nghĩa, đơn vị, độ phân giải,
  địa bàn, thời gian và các khóa ghép cần có. Bộ dữ liệu cho biến khí hậu không bị loại chỉ
  vì nó không đồng thời chứa biến kết quả nông nghiệp.
- Sau lượt đầu, những biến bắt buộc chưa có ứng viên được tìm bổ sung bằng cụm từ mở rộng.
  Nếu LLM không tạo được cụm từ, hệ thống dùng truy vấn ghép xác định từ kế hoạch đã duyệt.
- Một lần yêu cầu tìm lại giữ các snapshot đã tải, tìm thêm nguồn cho biến thiếu và kiểm tra
  chúng cùng nhau ở bước thiết kế. Nhật ký lưu lượt tìm, truy vấn, kho đã tìm, ứng viên,
  lỗi truy cập và biến mà từng ứng viên có thể phục vụ.
- Ứng viên metadata và nguồn đủ điều kiện phân tích là hai trạng thái khác nhau. Snapshot
  chỉ được ánh xạ sau khi cột tồn tại thật; vẫn phải xác nhận địa bàn, thời gian, đơn vị,
  độ phân giải, khả năng ghép và giấy phép. Không tìm thấy kết quả không chứng minh rằng
  dữ liệu không tồn tại.

## Giới hạn được áp dụng

- Nguồn hiện có: Hugging Face, Mendeley Data/DataCite và file do nhóm nghiên cứu tải lên. Chưa có connector trực tiếp cho
  FAOSTAT, NASA, ERA5 hoặc cổng thống kê quốc gia. Kiến trúc cho phép bổ sung connector;
  không tuyên bố đã truy cập những nguồn chưa tích hợp.
- Tối đa 10 snapshot ứng viên; thiết kế chọn tối đa 3 nguồn. Mặc định 6 snapshot,
  10.000 dòng/nguồn; người dùng có thể tăng tối đa 50.000 dòng.
- Kiểm tra/tải ở bước thu thập là để đánh giá tính khả thi; chưa chạy phân tích giả thuyết.
- Chỉ ghép trái theo khóa chung với `one_to_one` hoặc `many_to_one`; không ghép nhiều-nhiều,
  không tự suy ra khóa bằng số thứ tự dòng, không tự tổng hợp không gian/thời gian.
- Chuyển đổi khai báo: giữ nguyên, chuyển số, lấy năm từ ngày tháng, trim/casefold,
  chuyển đơn vị tuyến tính có hệ số và lý do được duyệt. Không chạy mã Python do LLM sinh.
- Lọc mẫu trước chuẩn hóa bằng `eq`, `in`, `between` trên cột gốc; lưu điều kiện và số dòng
  trước/sau lọc trong provenance. Không có bản ghi đúng điều kiện thì chặn phân tích.
- Phương pháp hiện có trong luồng tự động: mô tả, tương quan và OLS với biến số.
  Nếu nghiên cứu cần panel, chuỗi thời gian, mô hình không gian hoặc nhận diện nhân quả,
  thiết kế phải báo chưa được hỗ trợ. Kết quả quan sát không được trình bày như tác động nhân quả.
- Khớp metadata và mẫu dữ liệu không thay thế đối chiếu bảng mã, giấy phép, phạm vi mẫu
  và đánh giá chuyên môn. Kiểm chứng ngữ nghĩa dùng LLM, không phải bình duyệt độc lập.

## Khởi chạy

Cấu hình `.env` như các luồng HTTM hiện có: `DATABASE_URL`, `DEEPSEEK_API_KEY` và
`DEEPSEEK_MODEL` nếu cần. Không có API key thì các bước lập kế hoạch/viết không chạy được.

```powershell
docker compose up -d --build
```

Hoặc chạy trên máy đã có PostgreSQL:

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

Mở `http://127.0.0.1:8000/`. Việc tạo mới research plan gọi LLM trước khi xếp hàng;
các bước nghiên cứu tiếp theo chạy nền và được lưu sau từng bước.

## API chính

```http
POST /autonomous/runs
```

```json
{
  "topic": "Ảnh hưởng của biến đổi khí hậu đến năng suất lúa tại Việt Nam",
  "region": "Vietnam",
  "target_paper_count": 15,
  "max_sources": 6,
  "max_rows": 10000,
  "authors": [{"name": "Tên tác giả", "affiliation": "Đơn vị"}]
}
```

Hoặc gửi `project_id` thay cho `topic`. Project tổng quan (`review`) không dùng luồng thực nghiệm này.

- `GET /autonomous/runs`: danh sách tiến trình.
- `GET /autonomous/runs/{id}`: trạng thái, artifact, lịch sử quyết định và phiên bản.
- `POST /autonomous/runs/{id}/decision`:
  `{"expected_version": 9, "action": "approve", "notes": "Đồng ý phương án"}`.
- Điều chỉnh:
  `{"expected_version": 9, "action": "revise", "notes": "Giải thích yêu cầu", "restart_from": "research_plan"}`.
  Các điểm quay lại được phép phụ thuộc bước hiện tại: `research_plan`, `data_collection`,
  `design`, `introduction`. Không được bỏ qua điểm duyệt.
- `POST /autonomous/runs/{id}/resume` hoặc `/cancel`: gửi `{"expected_version": 9}`.
- `GET /autonomous/runs/{id}/manuscript.md`: bản thảo Markdown, có nhãn chưa duyệt khi cần.
- `GET /autonomous/runs/{id}/export.pdf`: PDF cuối sau khi kiểm chứng và duyệt.
- `GET /autonomous/runs/{id}/audit.json`: snapshot hiện tại và lịch sử phiên bản.

`expected_version` phải lấy từ phản hồi mới nhất; quyết định trên phiên bản cũ trả HTTP 409.

## Kiểm thử

```powershell
python -m pytest tests/test_autonomous.py tests/test_autonomous_uploads.py -q
python -m tests.autonomous_browser_smoke
```

`tests/test_autonomous_integration.py` chỉ chạy trên database kiểm thử cách ly có tài khoản
`httm_review_test`, cổng `65433`, giống kiểm thử tích hợp hiện có của dự án. Kiểm thử dùng
PostgreSQL, Parquet, ghép dữ liệu, thống kê và PDF thật; nguồn bên ngoài và LLM được giả lập.
Không dùng database làm việc của người dùng cho kiểm thử này.
