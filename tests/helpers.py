import json
from app.models import ResearchPlan, SearchStrategy, KeywordSynonyms


def sample_plan() -> ResearchPlan:
    return ResearchPlan(
        topic="AI và năng suất sinh viên",
        research_domain="Giáo dục đại học",
        keywords=["ai in education", "student productivity"],
        synonyms=[KeywordSynonyms(keyword="ai in education", synonyms=["educational AI"])],
        research_questions=["AI ảnh hưởng thế nào đến năng suất học tập?"],
        search_strategy=SearchStrategy(
            databases=["openalex"], date_range="2018-2026", language=["en"],
            study_types=["survey"], target_paper_count=10,
        ),
    )


async def insert_project(conn, plan=None) -> int:
    plan = plan or sample_plan()
    return await conn.fetchval(
        "INSERT INTO research_projects (topic, research_plan) VALUES ($1, $2::jsonb) RETURNING id;",
        plan.topic, json.dumps(plan.model_dump()),
    )


async def insert_paper(conn, project_id, title="Paper", abstract="Abstract text", doi=None, pdf_url=None) -> int:
    return await conn.fetchval(
        "INSERT INTO papers (project_id, title, abstract, doi, source, pdf_url) "
        "VALUES ($1,$2,$3,$4,'openalex',$5) RETURNING id;",
        project_id, title, abstract, doi, pdf_url,
    )
