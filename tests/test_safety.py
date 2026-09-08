from applypilot.autoapply import allowed_hh_url
from applypilot.llm import cache_key
from applypilot.session import validate_state


def test_hostname_is_strict():
    assert allowed_hh_url("https://hh.ru/vacancy/1")
    assert allowed_hh_url("https://api.hh.ru/vacancy/1")
    assert not allowed_hh_url("https://evil.example/hh.ru")
    assert not allowed_hh_url("http://hh.ru/vacancy/1")


def test_session_validation_is_local(tmp_path):
    ok, detail = validate_state(tmp_path / "missing.json")
    assert not ok and detail == "missing"


def test_llm_cache_changes_with_profile():
    item = {"id": "1", "description": "Python"}
    assert cache_key(item, {"name": "A"}, "m") != cache_key(item, {"name": "B"}, "m")

