import httpx
import pytest
from fastapi import HTTPException
from openai import APIStatusError
from pydantic import BaseModel

from app.services import llm


class Out(BaseModel):
    answer: str


class _O:
    pass


def _resp(content, finish="stop"):
    msg = _O(); msg.content = content
    ch = _O(); ch.message = msg; ch.finish_reason = finish
    r = _O(); r.choices = [ch]
    return r


def _status_error(code):
    req = httpx.Request("POST", "https://x")
    res = httpx.Response(code, request=req, json={"error": {"message": "boom"}})
    return APIStatusError("boom", response=res, body=None)


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []
        self.chat = self
        self.completions = self

    async def create(self, **kw):
        self.calls.append(kw)
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []

    async def fast(s):
        sleeps.append(s)
    monkeypatch.setattr(llm.asyncio, "sleep", fast)
    return sleeps


async def test_success(monkeypatch):
    fake = FakeClient([_resp('{"answer": "ok"}')])
    monkeypatch.setattr(llm, "_client", fake)
    assert (await llm.generate_structured("hi", Out)).answer == "ok"


async def test_retry_503_with_backoff(monkeypatch, no_sleep):
    fake = FakeClient([_status_error(503), _status_error(503), _resp('{"answer": "ok"}')])
    monkeypatch.setattr(llm, "_client", fake)
    assert (await llm.generate_structured("hi", Out)).answer == "ok"
    assert len(fake.calls) == 3
    assert no_sleep == [2, 4]


async def test_402_fails_fast(monkeypatch):
    fake = FakeClient([_status_error(402)])
    monkeypatch.setattr(llm, "_client", fake)
    with pytest.raises(HTTPException) as e:
        await llm.generate_structured("hi", Out)
    assert e.value.status_code == 502 and "402" in e.value.detail
    assert len(fake.calls) == 1


async def test_gives_up_after_max_attempts(monkeypatch):
    fake = FakeClient([_status_error(503)] * llm._MAX_ATTEMPTS)
    monkeypatch.setattr(llm, "_client", fake)
    with pytest.raises(HTTPException):
        await llm.generate_structured("hi", Out)
    assert len(fake.calls) == llm._MAX_ATTEMPTS


async def test_truncated_retries_with_shorter_prompt(monkeypatch):
    fake = FakeClient([_resp('{"answer": "x', finish="length"), _resp('{"answer": "ok"}')])
    monkeypatch.setattr(llm, "_client", fake)
    await llm.generate_structured("hi", Out)
    assert "NGẮN HƠN" not in fake.calls[0]["messages"][1]["content"]
    assert "NGẮN HƠN" in fake.calls[1]["messages"][1]["content"]


def test_python_style_null_fixed():
    assert llm._try_parse_json('{"a": Null}') == {"a": None}
