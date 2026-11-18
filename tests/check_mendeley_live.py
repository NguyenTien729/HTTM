"""Explicit live public-repository smoke test; no LLM calls or private data."""
import asyncio
import json
import httpx
from app.data_research.repositories import search_mendeley, inspect, download
from app.data_research.service import analyze_dataframe


async def main():
    context = {"project_topic": "Tác động của AI đến năng suất sinh viên đại học Việt Nam",
        "_search_concept_groups": [["artificial intelligence", "AI", "ChatGPT"],
            ["university students", "students", "undergraduate"],
            ["productivity", "academic performance", "academic progress", "smart learning"]],
        "_search_scope_terms": ["vietnam", "vietnamese", "viet nam"]}
    async with httpx.AsyncClient(timeout=20) as client:
        candidates = await search_mendeley(client, context, ["AI university students productivity"], 15)
    print(json.dumps([{"id": c["dataset_id"], "title": c["title"], "score": c["gap_match_score"], "scope": c["scope_match"]}
        for c in sorted(candidates, key=lambda c: c["gap_match_score"], reverse=True)[:8]], ensure_ascii=False))
    assert candidates
    preview = await inspect("mendeley:9krwng5dm7:1")
    selected = preview["preview_selection"]
    frame, meta = await download("mendeley:9krwng5dm7:1", selected["config"], selected["split"])
    result = analyze_dataframe(frame, meta)
    print(json.dumps({"rows": preview["num_rows_total"], "columns": [c["name"] for c in preview["columns"]],
                      "analyzed_rows": result["overview"]["rows_analyzed"]}, ensure_ascii=False))
    assert result["overview"]["rows_analyzed"] > 100


if __name__ == "__main__":
    asyncio.run(main())
