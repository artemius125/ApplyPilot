from applypilot.templates import create_template, list_templates


def test_public_templates_are_available_and_do_not_overwrite(tmp_path):
    assert set(list_templates()) == {
        "ai-llm", "ai-agents-llmops", "ml-engineering", "python-backend", "go-backend", "software-general"
    }
    output = tmp_path / "search.toml"
    assert create_template("go-backend", output) == output
    assert "preset = \"go-backend\"" in output.read_text(encoding="utf-8")

    try:
        create_template("go-backend", output)
    except FileExistsError:
        pass
    else:
        raise AssertionError("template creation overwrote an existing file")
