from __future__ import annotations

from .storage import Store


def report(store: Store, account: str = "default") -> str:
    counts = store.count(account)
    lines = [f"Unique application vacancies: {sum(counts.values())}",
             f"Application attempts/events: {store.event_count(account)}"]
    lines.extend(f"{status}: {counts[status]}" for status in sorted(counts))
    latest = store.latest_sync(account)
    if latest:
        lines.append(f"Last status sync: {latest['created_at']} ({latest['status']}, items={latest['item_count']})")
    else:
        lines.append("Last status sync: never")
    return "\n".join(lines)
