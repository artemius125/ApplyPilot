import pytest

from applypilot.cli import build_parser
from applypilot.config import ConfigError, effective_search, search_groups, validate_search


def test_effective_search_has_portable_defaults_and_cli_preset():
    result = effective_search({"areas": [1]}, "python-backend")

    assert result["areas"] == [1]
    assert result["max_pages"] == 20
    assert "Python Developer" in result["queries"]


def test_invalid_limits_are_rejected_before_network():
    with pytest.raises(ConfigError, match="max_pages"):
        validate_search({"max_pages": -1, "max_queries": 40, "salary": {}})


def test_score_does_not_bake_in_a_city_or_salary_requirement():
    from applypilot.scoring import score_vacancy

    result = score_vacancy({"id": "1", "name": "Python Developer", "area": "Москва"}, {})

    assert "location:other-city" not in result.hard_killers


def test_search_groups_keep_independent_regions_and_remote_rules():
    groups = search_groups({
        "queries": ["base"],
        "groups": [
            {"name": "city", "queries": ["Go"], "areas": [1], "only_remote": False},
            {"name": "country-remote", "queries": ["Go remote"], "areas": [113], "only_remote": True},
        ],
    })

    assert [(group["group_name"], group["queries"], group["areas"], group["only_remote"])
            for group in groups] == [
                ("city", ["Go"], [1], False), ("country-remote", ["Go remote"], [113], True)
            ]


def test_no_remote_cli_flag_is_an_explicit_false_override():
    args = build_parser().parse_args(["scan", "--no-remote"])

    assert args.remote is False


def test_config_show_accepts_an_explicit_portable_preset():
    args = build_parser().parse_args(["config", "show", "--preset", "go-backend"])

    assert args.preset == "go-backend"


def test_scan_add_query_preserves_a_preset_query_roster():
    args = build_parser().parse_args([
        "scan", "--preset", "ai-agents-llmops", "--add-query", "LLM Platform",
    ])

    assert args.preset == "ai-agents-llmops"
    assert args.add_query == ["LLM Platform"]


def test_request_budget_is_configurable_without_a_hidden_cap():
    result = effective_search({"request_budget": 900, "details_limit": 1200, "max_pages": 30})

    assert result["request_budget"] == 900
    assert result["details_limit"] == 1200
    assert result["max_pages"] == 30
