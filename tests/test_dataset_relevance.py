import asyncio
import pytest
from app.data_research import service

GAP = {"description": "Tác động của AI lên học tập của sinh viên Việt Nam",
       "proposed_research_question": "AI ảnh hưởng thế nào đến kết quả học tập của sinh viên Việt Nam?"}


def candidate(name, description=""):
    return service._to_candidate({"id": name, "cardData": {"description": description}}, service._gap_seed_queries(GAP))


def test_influence_is_not_an_image_and_vietnam_is_not_agriculture():
    queries = service._gap_seed_queries(GAP)
    assert not any("image classification" in q or "agriculture" in q for q in queries)
    assert not service._has_phrase("hinh anh huong dan", "anh chup")
    assert service._normalized_text("Đại học, tác động") == "dai hoc tac dong"


@pytest.mark.parametrize("name", [
    "sfarrukhm/intel-image-classification", "andrewsunanda/fast_food_image_classification",
    "fsuarez/autotrain-data-image-classification", "Kentaro-Machida/Matthiola-incana-Leaf-Image-Classification",
    "huggingface/image-classification-test-sample", "stevelohwc/pokemon_card_image_for_authenticity_classification",
    "yashikota/birds-525-species-image-classification", "NurzatS/Product_Image_Classification",
    "DarkyMan/nsfw-image-classification", "miladfa7/Intel-Image-Classification",
])
def test_rejects_user_reported_irrelevant_results(name):
    assert not service._candidate_relevance(candidate(name), GAP)


def test_relevance_uses_dynamic_concepts_not_a_fixed_education_filter():
    context = GAP | {"_search_concept_groups": [["ai", "chatgpt"], ["students", "sinh vien"], ["survey", "learning outcomes", "academic performance"]]}
    assert not service._candidate_relevance(candidate("example/student-grades", "Academic performance survey"), context)
    assert not service._candidate_relevance(candidate("example/chatgpt", "AI instruction tuning"), context)
    assert not service._candidate_relevance(candidate("example/education-llm", "AI education question answering benchmark"), context)
    partial = candidate("example/ai-survey", "Survey of ChatGPT usage and academic performance among university students in Vietnam")
    assert service._candidate_relevance(partial, context)
    assert partial["match_scope"] == "project_topic_metadata_match"
    local = candidate("example/vietnam-students-ai", "Vietnamese university students survey of ChatGPT usage and learning outcomes")
    assert service._candidate_relevance(local, context)


@pytest.mark.parametrize("topic,groups,description", [
    ("Dự báo năng suất lúa theo khí hậu", [["rice", "lua"], ["yield", "nang suat"], ["climate", "khi hau"]], "Rice yield and climate observations"),
    ("Phân loại ảnh chim", [["bird", "birds", "chim"], ["image classification", "phan loai anh"]], "Birds image classification dataset"),
    ("Phát hiện gian lận thẻ tín dụng", [["credit card"], ["fraud", "gian lan"]], "Credit card fraud transactions"),
    ("Chẩn đoán bệnh tim", [["heart", "tim"], ["disease", "benh"]], "Heart disease clinical measurements"),
])
def test_search_applies_to_any_project_domain(topic, groups, description):
    context = {"project_topic": topic, "description": "A generic population gap", "_search_concept_groups": groups}
    good = candidate("example/relevant", description)
    assert service._candidate_relevance(good, context)
    assert not service._candidate_relevance(candidate("example/other", "Survey of ChatGPT usage among university students"), context)
    assert good["relevance_evidence"]["project_topic"] == topic


def test_fallback_ranks_project_topic_not_an_unrelated_gap():
    context = {"project_topic": "credit card fraud detection", "description": "student learning outcomes"}
    assert service._candidate_relevance(candidate("example/transactions", "Credit card fraud transactions"), context)
    assert not service._candidate_relevance(candidate("example/students", "Student learning outcomes survey"), context)
    assert not any("student" in q for q in service._gap_seed_queries(context))


def test_llm_builds_concepts_from_project_topic(monkeypatch):
    from app.services import llm
    prompts = []
    async def generate(prompt, schema):
        prompts.append(prompt)
        return schema(queries=["credit card fraud"], concept_groups=[["credit card", "the tin dung"], ["fraud", "gian lan"], ["impact", "anh huong"]])
    monkeypatch.setattr(llm, "generate_structured", generate)
    context = {"project_topic": "Phát hiện gian lận thẻ tín dụng", "description": "Gap about limited populations"}
    queries = asyncio.run(service._expand_gap_queries(context, [], 1))
    assert queries == ["credit card fraud"]
    assert "Chủ đề project: Phát hiện gian lận thẻ tín dụng" in prompts[0]
    assert context["_search_concept_groups"] == [["credit card", "the tin dung"], ["fraud", "gian lan"]]


def test_discovery_does_not_fill_quota_with_irrelevant_or_unmatched_results(monkeypatch):
    async def read_card(*args):
        return ""
    monkeypatch.setattr(service, "_read_dataset_card", read_card)
    async def expand(*args):
        return ["image classification"]  # Even a bad expansion cannot override relevance.
    async def search(*args, **kwargs):
        return [{"id": "example/image-classification"}, {"id": "example/unrelated"}]
    monkeypatch.setattr(service, "_expand_gap_queries", expand)
    monkeypatch.setattr(service, "_get_json", search)
    result = asyncio.run(service.discover_gap_datasets(GAP, limit=10, max_search_seconds=5))
    assert result["count"] == 0
    assert result["datasets"] == []
    assert result["search"]["rejected_irrelevant_count"] == 2
    assert result["search"]["stop_reason"] == "query_exhausted"


@pytest.mark.parametrize("description,accepted", [
    ("AI adoption and agriculture productivity in Vietnam", True),
    ("Artificial intelligence for agriculture in Viet Nam", True),
    ("AI adoption and agriculture productivity in India", False),
    # No place named: kept as a candidate, but flagged for scope verification.
    ("AI adoption and agriculture productivity", True),
    ("Agriculture productivity in Vietnam", False),
    ("AI adoption among students in Vietnam", False),
])
def test_agricultural_ai_requires_both_topic_and_geography(description, accepted):
    context = {"project_topic": "Ảnh hưởng của AI tới nông nghiệp Việt Nam",
               "_search_concept_groups": [["ai", "artificial intelligence"], ["agriculture", "nong nghiep"]]}
    item = candidate("example/survey", description)
    assert service._candidate_relevance(item, context) is accepted
    if accepted:
        names_vietnam = "viet nam" in description.lower() or "vietnam" in description.lower()
        assert item["scope_verification_required"] is (not names_vietnam)


def test_source_about_another_country_is_rejected_unless_it_includes_the_target():
    context = {"project_topic": "Ảnh hưởng của AI tới nông nghiệp Việt Nam",
               "_search_concept_groups": [["ai"], ["agriculture"]]}
    rejected = candidate("example/survey", "AI and agriculture survey of farmers in China and India")
    assert service._candidate_relevance(rejected, context) is False
    assert rejected["_rejection_reasons"][0].startswith("metadata_explicitly_other_country")
    regional = candidate("example/survey", "AI and agriculture panel for Thailand, Vietnam and Indonesia")
    assert service._candidate_relevance(regional, context) is True


def test_all_required_concepts_must_match():
    context = {"project_topic": "AI agriculture productivity",
               "_search_concept_groups": [["ai"], ["agriculture"], ["productivity"]]}
    assert not service._candidate_relevance(candidate("example/survey", "AI agriculture benchmark"), context)


def test_vietnam_scope_survives_failed_or_empty_expansion():
    context = {"project_topic": "AI agriculture Việt Nam", "_search_scope_terms": []}
    unknown = candidate("example/survey", "AI agriculture")
    assert service._candidate_relevance(unknown, context)
    assert unknown["scope_verification_required"] is True
    named = candidate("example/survey", "AI agriculture Viet Nam")
    assert service._candidate_relevance(named, context)
    assert named["scope_verification_required"] is False
    assert not service._candidate_relevance(candidate("example/survey", "AI agriculture India"), context)


def test_hub_queries_include_short_domain_terms():
    context = {"project_topic": "AI agriculture Vietnam",
               "_search_concept_groups": [["ai", "artificial intelligence"], ["agriculture", "farming"]]}
    queries = service._hub_retrieval_queries(context, ["AI agriculture Vietnam"], [])
    assert queries[0] == "agriculture"
    assert "vietnam" in queries
    assert "ai" not in queries


def test_card_can_supply_missing_geographic_and_topic_evidence():
    context = {"project_topic": "AI agriculture Vietnam",
               "_search_concept_groups": [["ai", "machine learning"], ["agriculture"]]}
    item = candidate("example/rice-yield", "Rice observations")
    assert not service._candidate_relevance(item, context)
    item["_card_text"] = "Agriculture observations collected in Vietnam for machine learning."
    assert service._candidate_relevance(item, context)
    assert item["scope_match"] == "mentioned_in_metadata"


def test_synthetic_personas_are_not_an_agricultural_study_sample():
    context = {"project_topic": "AI agriculture Vietnam",
               "_search_concept_groups": [["ai"], ["agriculture"]]}
    item = candidate("example/personas-vietnam", "Synthetic profiles")
    item["_card_text"] = "AI agriculture occupations in Vietnam"
    assert not service._candidate_relevance(item, context)


def test_discovery_reads_card_before_rejecting_sparse_metadata(monkeypatch):
    from app.data_research import repositories

    async def expand(context, *args):
        context["_search_concept_groups"] = [["ai", "machine learning"], ["agriculture"]]
        return ["AI agriculture Vietnam"]

    async def search(*args, **kwargs):
        return [{"id": "example/rice-yield"}]

    async def read(*args):
        return "Machine learning for agriculture in Vietnam"

    async def external(*args):
        return []

    monkeypatch.setattr(service, "_expand_gap_queries", expand)
    monkeypatch.setattr(service, "_get_json", search)
    monkeypatch.setattr(service, "_read_dataset_card", read)
    monkeypatch.setattr(repositories, "search_mendeley", external)
    result = asyncio.run(service.discover_gap_datasets(
        {"project_topic": "AI agriculture Vietnam"}, limit=1, max_search_seconds=5))
    assert result["count"] == 1
    assert result["search"]["dataset_cards_checked"] == 1
    assert "_card_text" not in result["datasets"][0]
