"""Readable variable labels and vector figures from saved statistics, never invented data."""
import math
from reportlab.graphics.shapes import Drawing, Rect, Line, String
from reportlab.lib.colors import HexColor


def guide_for(snap):
    return snap.get("variable_guide") or (snap.get("dataset") or {}).get("variable_guide") or {}


def meanings(snap):
    return {v["name"]: v for v in guide_for(snap).get("variables", [])}


def label(snap, name):
    definition = meanings(snap).get(name, {})
    title = definition.get("label_vi")
    return f"{name} — {title}" if title and title != name else name


def value_label(snap, column, value):
    mapping = meanings(snap).get(column, {}).get("value_labels", {})
    key = str(value)
    if key not in mapping:
        try:
            number = float(key)
            if number.is_integer():
                key = str(int(number))
        except (ValueError, OverflowError):
            pass
    return f"{mapping[key]} (mã {value})" if key in mapping else str(value)


def numeric_variables(snap):
    definitions = meanings(snap)
    return [(name, summary) for name, summary in (snap.get("column_summaries") or {}).items()
            if summary.get("kind") == "numeric" and summary.get("n")
            and definitions.get(name, {}).get("kind") != "categorical"]


def results_explanations(snap):
    guide = guide_for(snap)
    paragraphs = []
    if guide.get("overview_vi"):
        paragraphs.append(guide["overview_vi"])
    numeric = dict(numeric_variables(snap))
    for group in guide.get("groups", []):
        items = [(c, numeric[c]) for c in group["columns"] if c in numeric and numeric[c].get("mean") is not None]
        if items:
            means = [s["mean"] for _, s in items]
            paragraphs.append(f"{group['label_vi']}: {group['description_vi']} Các mã gồm {', '.join(c for c, _ in items)}. "
                              f"Trung bình từng mục nằm trong khoảng {min(means):.2f}–{max(means):.2f}. "
                              "Đây là thống kê riêng của từng chỉ báo, chưa phải điểm tổng hợp hoặc thang đo đã được kiểm định.")
    if numeric:
        name, s = next(iter(numeric.items()))
        paragraphs.append(f"Cách đọc bảng: {label(snap, name)} có {s['n']:,} giá trị hợp lệ; trung bình là {s['mean']:.2f}. "
                          "Độ lệch chuẩn cho biết các giá trị phân tán quanh trung bình; trung vị là giá trị ở giữa sau khi sắp xếp. "
                          "Nhỏ nhất và lớn nhất là phạm vi quan sát, không mặc nhiên là giới hạn lý thuyết của thang đo. "
                              "Trung bình cao hơn chỉ có ý nghĩa theo cách mã hóa của nguồn, không tự chứng minh tác động của biến giải thích lên kết quả nghiên cứu.")
    if snap.get("top_correlations_pearson"):
        paragraphs.append("Cách đọc tương quan: hệ số r nằm từ −1 đến 1; dấu dương biểu thị hai biến có xu hướng tăng cùng chiều, "
                          "dấu âm biểu thị xu hướng ngược chiều. Độ lớn tuyệt đối mô tả mức liên hệ tuyến tính trong mẫu, không chứng minh nhân quả. "
                          "Bảng chỉ chọn các cặp có |r| lớn nhất; cặp không xuất hiện không có nghĩa là không có liên hệ. "
                          "Tương quan giữa hai mục trong cùng thang đo không trực tiếp trả lời mối liên hệ giữa các khái niệm nghiên cứu.")
    return paragraphs


def _short(text, size=50):
    return text if len(text) <= size else text[:size-1] + "…"


def figure_blocks(snap, font, width):
    """At most four item-mean panels and one dimensionless correlation panel."""
    numeric = dict(numeric_variables(snap))
    figures = []
    for group in guide_for(snap).get("groups", [])[:4]:
        items = [(name, numeric[name]) for name in group["columns"] if name in numeric and numeric[name].get("mean") is not None]
        if not items:
            continue
        # Each panel describes one source-defined group, never averages raw coded categories.
        items = items[:12]
        low = min(0., min(s.get("min", s["mean"]) for _, s in items))
        high = max(s.get("max", s["mean"]) for _, s in items)
        if high <= low:
            high = low + 1
        height = 60 + len(items)*23
        drawing = Drawing(width, height)
        left, right = 90, width-45
        span = right-left
        scale = lambda v: left + span*(v-low)/(high-low)
        drawing.add(Line(left, 33, right, 33, strokeColor=HexColor("#777777")))
        for tick in range(6):
            value = low+(high-low)*tick/5
            x = scale(value)
            drawing.add(Line(x, 30, x, 33, strokeColor=HexColor("#777777")))
            drawing.add(String(x, 17, f"{value:.1f}", fontName=font, fontSize=9, textAnchor="middle"))
        for i, (name, summary) in enumerate(items):
            y = height-25-i*23
            zero = scale(max(low, 0))
            end = scale(summary["mean"])
            drawing.add(String(left-8, y+2, name, fontName=font, fontSize=10, textAnchor="end"))
            drawing.add(Rect(min(zero,end), y, max(1,abs(end-zero)), 13, fillColor=HexColor("#4477AA"), strokeColor=None))
            drawing.add(String(end+5, y+2, f"{summary['mean']:.2f}", fontName=font, fontSize=10))
        drawing.add(String((left+right)/2, 2, "Trung bình từng mục — theo đơn vị gốc của dữ liệu", fontName=font, fontSize=9, textAnchor="middle"))
        figures.append((drawing, f"Trung bình các chỉ báo: {group['label_vi']}",
            "Mỗi thanh là một chỉ báo, không phải điểm tổng hợp. Trục thể hiện phạm vi quan sát, có bổ sung mốc 0; đối chiếu mã biến với bảng chú giải."))
    if not figures:
        for chart in [c for c in snap.get("charts", []) if c.get("type") == "histogram"][:2]:
            counts, edges = chart.get("counts", []), chart.get("edges", [])
            if not counts or len(edges) != len(counts)+1:
                continue
            height, left, bottom = 180, 45, 35
            drawing = Drawing(width, height)
            step = (width-left-20)/len(counts)
            maximum = max(1, max(counts))
            for i, count in enumerate(counts):
                drawing.add(Rect(left+i*step, bottom, max(1,step-1), count/maximum*120,
                                 fillColor=HexColor("#4477AA"), strokeColor=None))
            for i in range(6):
                index = round(i*len(counts)/5)
                drawing.add(String(left+index*step, 20, f"{edges[index]:.2f}", fontName=font, fontSize=8, textAnchor="middle"))
            drawing.add(String(5, 155, f"{maximum} quan sát", fontName=font, fontSize=8))
            drawing.add(String(width/2, 3, "Giá trị theo đơn vị gốc", fontName=font, fontSize=9, textAnchor="middle"))
            figures.append((drawing, "Phân bố giá trị: " + label(snap, chart["column"]),
                            "Mỗi cột là số quan sát trong một khoảng giá trị; không phải diễn biến theo thời gian."))
    categories = [(name, s) for name, s in (snap.get("column_summaries") or {}).items()
                  if s.get("kind") == "categorical" and s.get("top_values")]
    if categories:
        name, summary = categories[0]
        items = summary["top_values"][:10]
        height, left, right = 60+23*len(items), 165, width-45
        drawing = Drawing(width, height)
        maximum = max(1, max(item["count"] for item in items))
        for i, item in enumerate(items):
            y = height-25-i*23
            length = item["count"]/maximum*(right-left)
            drawing.add(String(left-8, y+2, _short(value_label(snap, name, item["value"]), 32), fontName=font, fontSize=9, textAnchor="end"))
            drawing.add(Rect(left, y, length, 13, fillColor=HexColor("#228833"), strokeColor=None))
            drawing.add(String(left+length+4, y+2, str(item["count"]), fontName=font, fontSize=9))
        drawing.add(String(width/2, 12, "Số quan sát theo nhóm", fontName=font, fontSize=9, textAnchor="middle"))
        figures.append((drawing, "Cơ cấu mẫu: " + label(snap, name), "Số lượng theo nhóm, tính trên các giá trị không thiếu; đối chiếu bảng tần số để xem tỷ lệ."))
    pairs = [p for p in snap.get("top_correlations_pearson", []) if isinstance(p.get("pearson_r"), (int,float)) and math.isfinite(p["pearson_r"])][:10]
    if pairs:
        height = 60 + 23*len(pairs)
        drawing = Drawing(width, height)
        left, right = 145, width-35
        mid = (left+right)/2
        scale = lambda r: mid+r*(right-left)/2
        drawing.add(Line(mid, 32, mid, height-9, strokeColor=HexColor("#777777")))
        for tick in (-1, -.5, 0, .5, 1):
            drawing.add(String(scale(tick), 17, f"{tick:g}", fontName=font, fontSize=9, textAnchor="middle"))
        for i, pair in enumerate(pairs):
            y = height-25-i*23
            value = pair["pearson_r"]
            end = scale(value)
            drawing.add(String(left-8, y+2, _short(pair["var_1"] + " – " + pair["var_2"], 30), fontName=font, fontSize=9, textAnchor="end"))
            drawing.add(Rect(min(mid,end), y, max(1, abs(end-mid)), 13, fillColor=HexColor("#4477AA" if value >= 0 else "#CC6677"), strokeColor=None))
            drawing.add(String(min(right-20,end+5), y+2, f"{value:.2f}", fontName=font, fontSize=9))
        drawing.add(String((left+right)/2, 2, "Hệ số tương quan Pearson (r)", fontName=font, fontSize=9, textAnchor="middle"))
        figures.append((drawing, "Các cặp chỉ báo có tương quan tuyến tính mạnh nhất",
            "Chỉ hiển thị các cặp đã lưu trong kết quả. Không suy ra cặp vắng mặt là không liên hệ; tương quan không phải quan hệ nhân quả."))
    return figures
