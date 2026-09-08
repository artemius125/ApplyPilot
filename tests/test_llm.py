import httpx

from applypilot import llm
from applypilot.llm import generate, rerank, rerank_cache_key


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _patch_client(monkeypatch, model_payload=None):
    instances = []
    catalog = model_payload or {"data": [{"id": "test-model"}]}
    completion = {"choices": [{"message": {"content": "Письмо"}}]}

    class Client:
        def __init__(self, *args, **kwargs):
            self.get_calls = []
            self.post_calls = []
            instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def get(self, url, **kwargs):
            self.get_calls.append((url, kwargs))
            return _Response(catalog)

        def post(self, url, **kwargs):
            self.post_calls.append((url, kwargs))
            return _Response(completion)

    monkeypatch.setattr(httpx, "Client", Client)
    return instances


def test_generate_success_on_cache_miss_uses_model_catalog_and_writes_cache(tmp_path, monkeypatch):
    instances = _patch_client(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    text, source = generate(
        {"id": "1", "name": "Backend", "description": "API"},
        {"name": "Candidate"},
        tmp_path,
        model="test-model",
        enabled=True,
    )

    assert (text, source) == ("Письмо", "generated")
    assert len(instances) == 1
    client = instances[0]
    assert client.get_calls[0][1]["headers"]["Authorization"] == "Bearer test-key"
    assert len(client.post_calls) == 1
    assert next(tmp_path.glob("*.txt")).read_text(encoding="utf-8") == "Письмо"


def test_generate_model_unavailable_does_not_call_completion(tmp_path, monkeypatch):
    instances = _patch_client(monkeypatch, {"data": [{"id": "other-model"}]})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    text, source = generate(
        {"id": "1"}, {}, tmp_path, model="test-model", enabled=True
    )

    assert (text, source) == ("", "unavailable")
    assert len(instances[0].get_calls) == 1
    assert instances[0].post_calls == []


def test_generate_returns_cached_response_without_provider_call(tmp_path, monkeypatch):
    item = {"id": "1", "name": "Backend", "description": "API"}
    profile = {"name": "Candidate"}
    model = "test-model"
    cache_path = tmp_path / f"{llm.cache_key(item, profile, model)}.txt"
    cache_path.write_text("Из кэша", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def fail_client(*args, **kwargs):
        raise AssertionError("provider must not be called for a cache hit")

    monkeypatch.setattr(httpx, "Client", fail_client)

    assert generate(item, profile, tmp_path, model=model, enabled=True) == ("Из кэша", "cache")


def test_generate_disabled_does_not_create_provider_client(tmp_path, monkeypatch):
    def fail_client(*args, **kwargs):
        raise AssertionError("disabled LLM must not construct a provider client")

    monkeypatch.setattr(httpx, "Client", fail_client)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert generate({"id": "1"}, {}, tmp_path, model="test-model", enabled=False) == ("", "disabled")


def test_empty_cache_is_replaced_with_generated_letter(tmp_path, monkeypatch):
    instances = _patch_client(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    item = {"id": "1"}
    path = tmp_path / f"{llm.cache_key(item, {}, 'test-model')}.txt"
    path.write_text(" \n", encoding="utf-8")

    assert generate(item, {}, tmp_path, model="test-model", enabled=True) == ("Письмо", "generated")
    assert len(instances[0].post_calls) == 1
    assert path.read_text(encoding="utf-8") == "Письмо"


def test_rerank_is_off_without_explicit_enable(tmp_path):
    items = [{"id": "1", "name": "Python", "description": "API"}]

    result, source = rerank(items, {}, tmp_path, "model", enabled=False)

    assert source == "disabled"
    assert result == items


def test_rerank_cache_key_changes_with_model_and_candidates():
    items = [{"id": "1", "name": "Python", "description": "API"}]

    assert rerank_cache_key(items, {}, "one") != rerank_cache_key(items, {}, "two")
    assert rerank_cache_key(items, {}, "one") != rerank_cache_key(
        [{"id": "1", "name": "Python", "description": "worker"}], {}, "one"
    )
