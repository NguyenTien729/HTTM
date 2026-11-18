from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from openai import APIStatusError

from app.services import llm
from app.routers import search
from app.services.relevance_filter import TopicConcepts
from tests.helpers import sample_plan


@pytest.mark.asyncio
async def test_balance_error_has_actionable_message_without_retry(monkeypatch):
    response = httpx.Response(402, request=httpx.Request("POST", "https://example.test"))
    call = AsyncMock(side_effect=APIStatusError("Insufficient Balance", response=response, body=None))
    monkeypatch.setattr(llm._client.chat.completions, "create", call)
    with pytest.raises(HTTPException, match="không đủ số dư") as caught:
        await llm.generate_structured("test", TopicConcepts)
    assert caught.value.status_code == 502
    assert call.await_count == 1


@pytest.mark.asyncio
async def test_search_preserves_provider_error(monkeypatch):
    monkeypatch.setattr(search, "_load_plan", AsyncMock(return_value=sample_plan()))
    error = HTTPException(502, "DeepSeek API không đủ số dư (HTTP 402)")
    monkeypatch.setattr(search, "extract_topic_concepts", AsyncMock(side_effect=error))
    with pytest.raises(HTTPException) as caught:
        await search.run_search(1)
    assert caught.value is error
