from applypilot.llm import rerank, rerank_cache_key


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
