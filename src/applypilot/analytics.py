from __future__ import annotations

from .storage import Store


def report(store: Store) -> str:
    counts = store.count()
    lines = [f"Unique application vacancies: {sum(counts.values())}",
             f"Application attempts/events: {store.event_count()}"]
    lines.extend(f"{status}: {counts[status]}" for status in sorted(counts))
    latest = store.latest_sync()
    if latest:
        lines.append(f"Last status sync: {latest['created_at']} ({latest['status']}, items={latest['item_count']})")
    else:
        lines.append("Last status sync: never")
    return "\n".join(lines)
