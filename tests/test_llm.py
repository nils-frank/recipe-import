"""Transportstufe der LLM-Extraktion (_post, llm.py), ohne Netzzugriff: `requests.post`
wird ersetzt, geprüft wird das Wiederholungsverhalten bei einer vorübergehenden
HTTP-Stufe des Anbieters (429/500/502/503/504) und die davon abgeleitete
LlmOverloadedError samt DESIGN.md §7 Wortlaut.
"""
from __future__ import annotations

import json

import pytest

import app as app_module
import llm

VALID_ANSWER = {
    "choices": [{"message": {"content": json.dumps({
        "name": "Kartoffelsuppe mit Majoran",
        "recipeIngredient": ["800 g Kartoffeln"],
        "recipeInstructions": ["Kartoffeln schälen."],
        "recipeYield": None,
        "totalTime": None,
        "description": None,
        "recipeCategory": [],
        "url": None,
    })}}]
}


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)


def test_a_single_503_is_retried_and_then_succeeds(monkeypatch):
    responses = [FakeResponse(503), FakeResponse(200, VALID_ANSWER)]
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return responses.pop(0)

    monkeypatch.setattr(llm.requests, "post", fake_post)

    content = llm._post([{"role": "user", "content": "x"}])

    assert json.loads(content)["name"] == "Kartoffelsuppe mit Majoran"
    assert len(calls) == 2


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_status_twice_raises_llm_overloaded_error(monkeypatch, status):
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: FakeResponse(status))

    with pytest.raises(llm.LlmOverloadedError):
        llm._post([{"role": "user", "content": "x"}])


def test_llm_overloaded_error_gets_the_spec_wording():
    exc = llm.LlmOverloadedError("Das Sprachmodell antwortete mit HTTP 503: ...")

    assert app_module._describe_source_error(exc) == (
        "Das Sprachmodell ist gerade überlastet. Bitte später erneut teilen."
    )


def test_a_non_retryable_status_fails_on_the_first_attempt(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return FakeResponse(400)

    monkeypatch.setattr(llm.requests, "post", fake_post)

    with pytest.raises(llm.LlmError) as exc_info:
        llm._post([{"role": "user", "content": "x"}])

    assert not isinstance(exc_info.value, llm.LlmOverloadedError)
    assert len(calls) == 1
