#!/usr/bin/env python3
"""Smoke test for Mission Control dispatch priority ordering."""
import issue_queue_db


def item(number, labels):
    return {
        "id": f"repo/x#{number}",
        "repo": "repo/x",
        "issue_number": number,
        "title": f"Issue {number}",
        "labels": labels,
        "enqueued_at": f"2026-01-01T00:00:{60-number:02d}Z",
    }


def main():
    # Higher explicit priority wins even if defined later.
    high = item(36, ["documentation", "epic", "priority:high"])
    epic1 = item(1, ["epic", "infrastructure"])
    story34 = item(34, ["story", "dashboard"])
    ordered = sorted([story34, high, epic1], key=issue_queue_db.priority_sort_key)
    assert [x["issue_number"] for x in ordered] == [36, 1, 34], ordered

    # Equal priority follows first-defined GitHub issue order, not sync enqueue order.
    equal = [item(34, ["story"]), item(2, ["epic"]), item(1, ["epic"]), item(10, ["story"])]
    ordered = sorted(equal, key=issue_queue_db.priority_sort_key)
    assert [x["issue_number"] for x in ordered] == [1, 2, 10, 34], ordered

    assert issue_queue_db.compute_priority(high) > issue_queue_db.compute_priority(epic1)
    print("ok")


if __name__ == "__main__":
    main()
