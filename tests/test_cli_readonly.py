import json

from applypilot.cli import main
from applypilot.storage import Store


def test_dry_run_does_not_create_or_change_journal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    input_path = tmp_path / "vacancies.json"
    input_path.write_text(json.dumps({"items": [{"id": "1", "name": "Python Engineer"}]}), encoding="utf-8")
    data_dir = tmp_path / "private" / "data"
    store = Store(data_dir / "applypilot.sqlite3")
    store.record({"id": "1", "name": "Python Engineer"}, "success")
    before = store.statuses()
    assert main(["--data-dir", str(data_dir), "apply", "--input", str(input_path), "--dry-run"]) == 0
    assert store.statuses() == before


def test_review_uses_the_requested_preset_without_touching_the_journal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    input_path = tmp_path / "vacancies.json"
    input_path.write_text(json.dumps({"items": [{"id": "1", "name": "Go Developer"}]}), encoding="utf-8")
    data_dir = tmp_path / "private" / "data"
    captured = {}

    def fake_write_review(items, profile, output, top):
        captured.update({"items": items, "profile": profile, "output": output, "top": top})
        return output

    monkeypatch.setattr("applypilot.cli.write_review", fake_write_review)
    assert main(["--data-dir", str(data_dir), "review", "--input", str(input_path),
                 "--preset", "go-backend"]) == 0
    assert "Go Developer" in captured["profile"]["search"]["queries"]
    assert not (data_dir / "applypilot.sqlite3").exists()
