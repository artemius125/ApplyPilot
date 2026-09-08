import csv

from applypilot.storage import Store


def test_legacy_import_is_idempotent(tmp_path):
    path = tmp_path / "apply_log.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["vacancy_id", "name", "status", "timestamp"])
        writer.writeheader()
        writer.writerow({"vacancy_id": "1", "name": "A", "status": "timeout", "timestamp": "2026-01-01"})
    store = Store(tmp_path / "state.sqlite3")
    assert store.import_csv(path) == 1
    assert store.import_csv(path) == 1
    assert store.statuses()["1"] == "unknown"
    assert store.count()["unknown"] == 1
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_import_timeout_then_success_keeps_final_success(tmp_path):
    path = tmp_path / "apply_log.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["vacancy_id", "status", "timestamp"])
        writer.writeheader()
        writer.writerow({"vacancy_id": "1", "status": "timeout", "timestamp": "2026-01-01T00:00:00+00:00"})
        writer.writerow({"vacancy_id": "1", "status": "success", "timestamp": "2026-01-02T00:00:00+00:00"})
    store = Store(tmp_path / "state.sqlite3")
    store.import_csv(path)
    assert store.statuses()["1"] == "success"
    assert store.count()["success"] == 1


def test_reservation_uses_one_budget_unit_and_deduplicates(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    item = {"id": "1", "name": "A"}
    assert store.reserve(item, "run", per_run=1, per_day=5)[0]
    assert not store.reserve(item, "run", per_run=1, per_day=5)[0]
    assert not store.reserve({"id": "2", "name": "B"}, "run", per_run=1, per_day=5)[0]
    store.record(item, "success", run_id="run")
    assert store.statuses()["1"] == "success"


def test_missing_timestamp_import_is_idempotent(tmp_path):
    path = tmp_path / "apply_log.csv"
    path.write_text("vacancy_id,status\n1,timeout\n", encoding="utf-8")
    store = Store(tmp_path / "state.sqlite3")
    store.import_csv(path)
    store.import_csv(path)
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
