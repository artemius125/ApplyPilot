from __future__ import annotations

import pytest

from applypilot.letters import LettersError, generate_letter


class _FakeResponse:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _profile() -> dict:
    return {
        "name": "Артём",
        "location": "Удалённо",
        "answers": {"motivation": "интересна прикладная работа с LLM"},
        "professional": {
            "summary": "Собираю LLM-агентов и автоматизацию на Python.",
            "skills": ["Python", "LLM", "FastAPI"],
            "experience": [
                {"company": "Acme", "role": "AI Engineer", "period": "2023–2024",
                 "achievements": ["собрал RAG-пайплайн"]},
            ],
        },
    }


def _item() -> dict:
    return {"id": "1", "name": "AI Agent Engineer", "company": "Beta",
            "experience": "1–3 года", "description": "LLM, RAG, FastAPI"}


def test_generate_letter_and_caches(tmp_path):
    calls = {"n": 0}
    letter = "Здравствуйте!\n\nМеня заинтересовала ваша вакансия.\n\nАртём"

    def fake_post(url, json, headers, timeout):
        calls["n"] += 1
        assert headers["Authorization"] == "Bearer test-key"
        return _FakeResponse(letter)

    first = generate_letter(_item(), _profile(), tmp_path,
                            api_key="test-key", post=fake_post)
    assert first["source"] == "generated"
    assert first["text"] == letter
    assert calls["n"] == 1

    # Second run must hit the cache and not call the model again.
    second = generate_letter(_item(), _profile(), tmp_path,
                             api_key="test-key", post=fake_post)
    assert second["source"] == "cache"
    assert second["text"] == letter
    assert calls["n"] == 1


def test_generate_letter_empty_response(tmp_path):
    def fake_post(url, json, headers, timeout):
        return _FakeResponse("   ")

    with pytest.raises(LettersError):
        generate_letter(_item(), _profile(), tmp_path,
                        api_key="test-key", post=fake_post, deadline=0.5)


def test_generate_letter_requires_key(tmp_path):
    with pytest.raises(LettersError):
        generate_letter(_item(), _profile(), tmp_path,
                        api_key="", post=lambda *a, **k: None)
