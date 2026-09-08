import csv

from applypilot.storage import Store


def test_legacy_import_is_idempotent(tmp_path):
    path = tmp_path / "apply_log.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["vacancy_id", "name", "status", "timestamp"])
        writer.writeheader()
        writer.writerow({"vacancy_id": "1", "name": "A", "status": "timeout", "timestamp": "2026-01-01"})
    store = Store(tmp_path / "state.sqlite3")
    first = store.import_csv(path)
    second = store.import_csv(path)
    assert first.logical_rows == 1
    assert first.events_added == 1
    assert second.already_imported is True
    assert second.events_added == 0
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


def test_manual_outcome_releases_transient_reservation_for_a_future_run(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    item = {"id": "1", "name": "A"}

    assert store.reserve(item, "first", per_run=1, per_day=2)[0]
    store.record(item, "needs_manual", run_id="first")

    assert store.reserve(item, "second", per_run=1, per_day=2)[0]


def test_missing_timestamp_import_is_idempotent(tmp_path):
    path = tmp_path / "apply_log.csv"
    path.write_text("vacancy_id,status\n1,timeout\n", encoding="utf-8")
    store = Store(tmp_path / "state.sqlite3")
    store.import_csv(path)
    store.import_csv(path)
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_blocked_ids_exclude_only_terminal_or_ambiguous_outcomes(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    for vacancy_id, status in {
        "success": "success", "already": "already_applied", "unknown": "unknown",
        "skipped": "skipped", "manual": "needs_manual", "failed": "failed_before_submit",
    }.items():
        store.record({"id": vacancy_id}, status)

    assert store.blocked_ids() == {"success", "already", "unknown"}


def test_run_items_keep_the_exact_dry_run_list(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    item = {"id": "1", "name": "A", "resume": "Resume"}
    store.start_run("dry", "account", "dry-run", tmp_path / "input.json", 10, [item])
    store.mark_run_item("dry", "1", "prepared", "would apply")
    store.finish_run("dry", "completed")

    summary = store.run_summary("dry")
    assert summary is not None
    assert summary["run"]["status"] == "completed"
    assert summary["counts"] == {"prepared": 1}


def test_negotiation_reconciles_unknown_without_replaying_the_run(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    items = [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]
    store.start_run("live", "account", "apply", tmp_path / "input.json", 2, items)
    store.record(items[0], "unknown", "submission not confirmed", "live", "account")
    store.finish_run("live", "stopped_unknown", "unknown result for vacancy 1")
    store.replace_negotiation_statuses(
        [{"vacancy_id": "1", "status": "not_viewed"}], "account"
    )

    assert store.reconcile_unknowns_from_negotiations("account") == ["1"]
    assert store.statuses("account")["1"] == "success"
    summary = store.run_summary("live")
    assert summary is not None
    assert summary["run"]["status"] == "stopped_reconciled"
    assert summary["counts"] == {"prepared": 1, "success": 1}
    assert store.reconcile_unknowns_from_negotiations("account") == []
