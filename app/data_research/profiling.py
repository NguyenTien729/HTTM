"""Role-aware, sample-scoped quality reports and chart data."""
import numpy as np
import pandas as pd
from scipy import stats


def prepare_analysis(frame, overrides):
    from .service import DataResearchError
    allowed = {"identifier", "categorical", "continuous", "text", "datetime"}
    if any(c not in frame or role not in allowed for c, role in overrides.items()):
        raise DataResearchError("Vai trò hoặc tên cột không hợp lệ.", 422)
    roles, quality = {}, []
    for c in frame:
        v = frame[c]
        numeric = pd.api.types.is_numeric_dtype(v) and not pd.api.types.is_bool_dtype(v)
        role = "continuous" if numeric else "categorical"
        if pd.api.types.is_datetime64_any_dtype(v):
            role = "datetime"
        elif str(c).lower().split(".")[-1] == "id" or str(c).lower().endswith("_id"):
            role = "identifier"
        elif str(c).lower().split(".")[-1] in {"label", "class", "category"} or str(c).lower().endswith("_label"):
            role = "categorical"
        elif not numeric and v.dropna().astype(str).str.len().mean() > 80:
            role = "text"
        roles[c] = overrides.get(c, role)
        blank = v.map(lambda x: isinstance(x, str) and not x.strip())
        infinities = int(np.isinf(v).sum()) if numeric else 0
        cleaned = v.mask(blank)
        if numeric:
            cleaned = cleaned.replace([np.inf, -np.inf], np.nan)
        if roles[c] == "continuous":
            converted = pd.to_numeric(cleaned, errors="coerce")
            if (cleaned.notna() & converted.isna()).any():
                raise DataResearchError(f"Cột {c} chứa giá trị không phải số.", 422)
            cleaned = converted
        if roles[c] == "datetime":
            converted = pd.to_datetime(cleaned, errors="coerce", utc=True)
            if (cleaned.notna() & converted.isna()).any():
                raise DataResearchError(f"Cột {c} chứa thời gian không hợp lệ.", 422)
            cleaned = converted
        frame[c] = cleaned
        item = {"column": c, "role": roles[c], "blank_count": int(blank.sum()),
                "infinite_count": infinities, "constant": cleaned.nunique() <= 1,
                "missing_count": int(cleaned.isna().sum())}
        if roles[c] == "continuous":
            q1, q3 = cleaned.quantile(.25), cleaned.quantile(.75)
            item["iqr_outlier_count"] = int(((cleaned < q1-1.5*(q3-q1)) | (cleaned > q3+1.5*(q3-q1))).sum())
        if roles[c] == "text":
            text = cleaned.dropna().astype(str)
            item.update(mean_length=float(text.str.len().mean()) if len(text) else None,
                        duplicate_non_missing=int(text.duplicated().sum()))
        if roles[c] == "datetime":
            item.update(min=cleaned.min().isoformat() if cleaned.notna().any() else None,
                        max=cleaned.max().isoformat() if cleaned.notna().any() else None)
        quality.append(item)
    return frame, roles, quality


def enrich_analysis(result, frame, roles, quality, correlation_cols, group, target):
    from .service import _json_safe
    result["column_roles"] = roles
    result["quality_report"] = {"scope": "analyzed_rows", "columns": quality,
        "note": "Thiếu, trùng và ngoại lệ được tính trên các dòng phân tích; không tự loại ngoại lệ."}
    result["correlation_columns"] = correlation_cols
    result["correlation_spearman"] = frame[correlation_cols].corr(method="spearman").to_dict() if len(correlation_cols) > 1 else None
    result["correlation_pair_counts"] = {a: {b: int(frame[[a, b]].notna().all(axis=1).sum()) for b in correlation_cols} for a in correlation_cols}
    charts = []
    for c, role in roles.items():
        v = frame[c].dropna()
        if role == "continuous" and len(v):
            counts, edges = np.histogram(v.to_numpy(dtype=float), bins=20)
            charts.append({"type": "histogram", "column": c, "counts": counts.tolist(), "edges": edges.tolist()})
        elif role == "categorical":
            counts = v.astype(str).value_counts().head(10)
            charts.append({"type": "bar", "column": c, "labels": counts.index.tolist(), "counts": counts.tolist()})
    if group in frame and target in frame and roles.get(target) == "continuous":
        boxes = []
        for name, subset in frame.groupby(group, observed=True):
            values = subset[target].dropna()
            if len(values):
                boxes.append({"group": str(name), "n": len(values), "quantiles": values.quantile([0, .25, .5, .75, 1]).tolist()})
        charts.append({"type": "boxplot", "column": target, "groups": boxes[:30]})
    if result["correlation_pearson"] is not None:
        charts.append({"type": "heatmap", "columns": correlation_cols, "matrix": result["correlation_pearson"]})
    result["charts"] = charts
    test = result.get("group_test")
    if test and test.get("status") == "ok":
        tests = test.get("tests", [])
        if tests:
            test["primary_test"] = tests[0]["name"] if len(test.get("group_summary", [])) == 2 else "Kruskal–Wallis"
            ps = [t["p_value"] for t in tests]
            if all(p is not None for p in ps):
                for t, adjusted in zip(tests, stats.false_discovery_control(ps, method="by")):
                    t["p_value_adjusted"] = float(adjusted)
                test["multiplicity"] = "Benjamini–Yekutieli; family = reported group tests"
            test["assumptions"] = {"independent_observations": "requires_human_confirmation",
                "note": "Welch so sánh trung bình; kiểm định hạng so sánh phân phối. ANOVA thường là kết quả bổ sung, cần giả định phương sai bằng nhau."}
        if group in frame and target in frame:
            used = frame[[group, target]].dropna()
            test["rows_excluded"] = len(frame) - len(used)
            if roles.get(target) == "continuous":
                sizes = used.groupby(group, observed=True).size()
                test["excluded_groups"] = [str(k) for k, n in sizes.items() if n < 2]
                test["rows_excluded"] += int(sizes[sizes < 2].sum())
                ordered = [(str(name), v[target].to_numpy(float)) for name, v in used.groupby(group, observed=True) if len(v) >= 2]
                groups = [v for _, v in ordered]
                if len(groups) == 2:
                    a, b = groups
                    difference = float(a.mean() - b.mean())
                    va, vb = a.var(ddof=1)/len(a), b.var(ddof=1)/len(b)
                    se = np.sqrt(va+vb)
                    df = (va+vb)**2/(va**2/(len(a)-1)+vb**2/(len(b)-1)) if va+vb else None
                    margin = stats.t.ppf(.975, df)*se if df else None
                    test["effect"] = {"group_order": [name for name, _ in ordered], "mean_difference_first_minus_second": difference,
                        "ci_95": [difference-margin, difference+margin] if margin is not None else None}
            elif test.get("test"):
                table = pd.crosstab(used[group], used[target])
                chi2, _, _, expected = stats.chi2_contingency(table)
                denominator = table.to_numpy().sum() * min(table.shape[0]-1, table.shape[1]-1)
                test["effect"] = {"cramers_v": float(np.sqrt(chi2/denominator)) if denominator else None}
                test["assumptions"] = {"expected_below_5_percent": float((expected < 5).mean()*100),
                    "minimum_expected_frequency": float(expected.min())}
                if table.shape == (2, 2) and (expected < 5).any():
                    fisher = stats.fisher_exact(table.to_numpy())
                    test["primary_test"] = "Fisher exact"
                    test["fisher_exact"] = {"odds_ratio": float(fisher.statistic), "p_value": float(fisher.pvalue)}
    regression = result.get("ols_regression")
    if regression and regression.get("status") == "ok":
        regression["rows_excluded"] = len(frame) - regression["n"]
        predictors = regression["predictors"]
        work = frame[[target] + predictors].dropna()
        design = np.column_stack([np.ones(len(work)), work[predictors].to_numpy(float)])
        y = work[target].to_numpy(float)
        beta = np.linalg.lstsq(design, y, rcond=None)[0]
        residuals = y-design@beta
        inverse = np.linalg.pinv(design.T@design)
        leverage = np.sum((design@inverse)*design, axis=1)
        adjusted = residuals / np.maximum(1-leverage, np.finfo(float).eps)
        covariance = inverse @ (design.T @ ((adjusted**2)[:, None]*design)) @ inverse
        se = np.sqrt(np.maximum(np.diag(covariance), 0))
        critical = stats.t.ppf(.975, len(work)-design.shape[1])
        for row, estimate, error in zip(regression["coefficients"], beta, se):
            row["hc3_std_error"] = float(error)
            row["hc3_ci_95"] = [float(estimate-critical*error), float(estimate+critical*error)]
        regression["diagnostics"] = {"condition_number": float(np.linalg.cond(design)),
            "max_leverage": float(leverage.max()), "residual_mean": float(residuals.mean())}
        regression["inference_note"] = "Hệ số có thêm sai số chuẩn HC3; p-value gốc dùng sai số chuẩn OLS thông thường. Cần kiểm tra độc lập quan sát và cấu trúc mô hình."
    gap = result["dataset"].get("research_gap")
    result["analysis_plan"] = {"research_gap": gap, "target": target, "group": group,
        "predictors": (regression or {}).get("predictors", []),
        "suitability_status": "requires_human_review",
        "review_questions": ["Các biến có đo đúng khái niệm của gap không?", "Đối tượng, ngôn ngữ và thời gian có phù hợp không?"],
        "unmeasured_requirements": "Chưa xác nhận; cần người nghiên cứu ánh xạ yêu cầu của gap vào cột."}
    result["methods_draft"] += " Chuỗi rỗng và giá trị vô hạn được xem là thiếu. Vai trò biến cần được người dùng xác nhận."
    result["results_draft"] += "\n\nCác chỉ số chất lượng và số dòng trùng chỉ áp dụng cho các dòng được phân tích."
    if test and test.get("multiplicity"):
        result["results_draft"] += " P-value hiệu chỉnh BY được cung cấp trong bảng kiểm định; p-value trong đoạn văn là giá trị gốc."
    return _json_safe(result)
