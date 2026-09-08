from applypilot.quality import run_benchmark


def test_benchmark_rejects_noise_and_accepts_role_examples():
    result = run_benchmark()

    assert result["accuracy"] == 1.0
    assert result["precision"] == 1.0
    assert result["recall"] == 1.0


def test_ai_designer_is_not_a_technical_role_match():
    result = run_benchmark()

    designer = next(row for row in result["rows"]
                    if row["id"] and row["kind"] == "noise" and row["preset"] == "ai-llm"
                    and row["id"] != "")
    assert designer["accepted"] is False


def test_benchmark_has_calibration_control_and_all_public_presets():
    result = run_benchmark(control_only=False)

    assert result["cases"] == 120
    assert result["precision"] >= 0.9
    assert result["recall"] >= 0.85
    assert set(result["by_preset"]) == {
        "ai-llm", "ai-agents-llmops", "ml-engineering", "python-backend", "go-backend", "software-general"
    }
