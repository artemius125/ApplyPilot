from applypilot.presets import resolve_search


def test_preset_is_used_when_private_config_does_not_override_queries():
    result = resolve_search({}, "go-backend")

    assert "Go Developer" in result["queries"]
    assert "golang" in result["include_terms"]


def test_private_search_values_override_preset():
    result = resolve_search({"preset": "ai-llm", "queries": ["Custom role"], "areas": [54]})

    assert result["queries"] == ["Custom role"]
    assert result["areas"] == [54]


def test_explicit_cli_preset_uses_only_declared_additional_queries():
    result = resolve_search({"queries": ["Private role"], "areas": [54]}, "go-backend")

    assert result["queries"] == ["Go Developer", "Golang Developer", "Go Backend", "Go Engineer"]
    assert result["areas"] == [54]

    extended = resolve_search({"additional_queries": ["Private role"]}, "go-backend")
    assert extended["queries"][-1] == "Private role"


def test_ai_agents_llmops_preset_is_narrower_than_the_general_ai_preset():
    result = resolve_search({}, "ai-agents-llmops")

    assert "LLMOps Engineer" in result["queries"]
    assert "qa" in result["exclude_titles"]
    assert "security" in result["exclude_titles"]
