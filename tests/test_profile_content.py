import json

import httpx
import pytest

from applypilot.config import ConfigError, professional_context
from applypilot.llm import _prompt, _rerank_prompt, cache_key, generate


def test_professional_context_keeps_rich_allowlisted_content_and_omits_empty_values():
    profile = {
        "professional": {
            "summary": "Backend engineer",
            "skills": ["Go", "PostgreSQL"],
            "resume_text": "Built reliable APIs.",
            "experience": [
                {
                    "company": "Acme",
                    "role": "Engineer",
                    "period": "2022-2024",
                    "description": "Owned API services.",
                    "achievements": ["Cut latency by 30%"],
                    "salary": "secret",
                }
            ],
            "projects": [
                {
                    "name": "ApplyPilot",
                    "role": "Author",
                    "description": "Automated applications.",
                    "technologies": ["Python"],
                    "achievements": ["Shipped offline-first workflow"],
                    "token": "secret",
                }
            ],
            "email": "private@example.com",
        },
        "password": "secret",
    }

    assert professional_context(profile) == {
        "summary": "Backend engineer",
        "skills": ["Go", "PostgreSQL"],
        "resume_text": "Built reliable APIs.",
        "experience": [
            {
                "company": "Acme",
                "role": "Engineer",
                "period": "2022-2024",
                "description": "Owned API services.",
                "achievements": ["Cut latency by 30%"],
            }
        ],
        "projects": [
            {
                "name": "ApplyPilot",
                "role": "Author",
                "description": "Automated applications.",
                "technologies": ["Python"],
                "achievements": ["Shipped offline-first workflow"],
            }
        ],
    }


def test_professional_context_rejects_invalid_nested_type():
    with pytest.raises(ConfigError, match=r"professional\.experience\[0\]\.achievements must be an array"):
        professional_context({"professional": {"experience": [{"achievements": "wrong"}]}})


def test_public_profile_and_prompt_include_grounded_professional_context_and_resume():
    item = {
        "id": "1",
        "name": "Backend Engineer",
        "description": "Build APIs",
        "resume": "Python Backend 2026",
    }
    profile = {
        "name": "Candidate",
        "professional": {
            "skills": ["Go"],
            "projects": [{"name": "ApplyPilot", "achievements": ["Shipped it"]}],
            "resume_file": "/private/resume.md",
        },
        "account": "private-account",
        "answers": {"salary": "private-salary"},
    }

    prompt = _prompt(item, profile)

    assert "Python Backend 2026" in prompt
    assert "ApplyPilot" in prompt
    assert "Shipped it" in prompt
    transmitted = json.loads(prompt.split("PROFILE:\n", 1)[1].split("\nVACANCY DATA:", 1)[0])
    assert transmitted["professional"]["skills"] == ["Go"]
    assert "private-account" not in prompt
    assert "private-salary" not in prompt
    assert "/private/resume.md" not in prompt


def test_rerank_does_not_receive_cover_letter_professional_context():
    prompt = _rerank_prompt([{"id": "1"}], {
        "name": "Candidate", "professional": {"resume_text": "Private full resume"}})

    assert "Candidate" in prompt
    assert "Private full resume" not in prompt


def test_letter_cache_key_changes_with_professional_context_and_selected_resume():
    item = {"id": "1", "name": "Backend", "description": "API", "resume": "Resume A"}
    profile = {"name": "Candidate"}

    assert cache_key(item, profile, "model") != cache_key(
        item, {"name": "Candidate", "professional": {"summary": "Engineer"}}, "model"
    )
    assert cache_key(item, profile, "model") != cache_key(
        {**item, "resume": "Resume B"}, profile, "model"
    )


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _patch_empty_completion_client(monkeypatch):
    instances = []

    class Client:
        def __init__(self, *args, **kwargs):
            self.post_calls = []
            instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def get(self, url, **kwargs):
            return _Response({"data": [{"id": "test-model"}]})

        def post(self, url, **kwargs):
            self.post_calls.append((url, kwargs))
            return _Response({"choices": [{"message": {"content": "  \n"}}]})

    monkeypatch.setattr(httpx, "Client", Client)
    return instances


def test_empty_generated_letter_is_unavailable_and_never_cached(tmp_path, monkeypatch):
    _patch_empty_completion_client(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    result = generate(
        {"id": "1"}, {}, tmp_path, model="test-model", enabled=True
    )

    assert result == ("", "unavailable")
    assert list(tmp_path.glob("*.txt")) == []


def test_required_empty_generated_letter_raises(tmp_path, monkeypatch):
    _patch_empty_completion_client(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    with pytest.raises(RuntimeError, match="generated cover letter is empty"):
        generate({"id": "1"}, {}, tmp_path, model="test-model", enabled=True, required=True)
