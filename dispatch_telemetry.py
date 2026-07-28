#!/usr/bin/env python3
"""Exact, auditable telemetry for Hermes gateway dispatches.

Token fields are populated only from a provider usage object. Missing or
unsupported fields remain NULL in SQLite and serialize as ``not_available``;
this module never derives tokens from prompts, files, or text length.

Cost values are internal. The public report exposes completeness counts only,
while the internal report retains the provider source, price version, and
formula supplied with the exact cost.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


NOT_AVAILABLE = "not_available"
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "total_tokens",
)
USAGE_FIELDS = TOKEN_FIELDS + ("api_calls",)
TELEMETRY_DB = Path(os.environ.get(
    "CWC_DISPATCH_TELEMETRY_DB",
    str(Path.home() / ".hermes" / "issue-queue" / "queue-state.db"),
))

SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_telemetry (
    dispatch_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    repo TEXT,
    task_class TEXT,
    model_provider TEXT,
    model TEXT,
    status TEXT NOT NULL DEFAULT 'dispatched',
    pr_number INTEGER,
    accepted_outcome_id TEXT,
    accepted_outcome_status TEXT NOT NULL DEFAULT 'not_available',
    accepted_outcome_source_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    duration_status TEXT NOT NULL DEFAULT 'not_available',
    duration_source_json TEXT NOT NULL DEFAULT '{}',
    usage_status TEXT NOT NULL DEFAULT 'not_available',
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_input_tokens INTEGER,
    reasoning_tokens INTEGER,
    total_tokens INTEGER,
    api_calls INTEGER,
    usage_source_json TEXT NOT NULL DEFAULT '{}',
    cost_status TEXT NOT NULL DEFAULT 'not_available',
    cost_micros INTEGER,
    cost_currency TEXT,
    cost_source_json TEXT NOT NULL DEFAULT '{}',
    cost_price_version TEXT,
    cost_formula TEXT,
    cost_price_inputs_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispatch_telemetry_repo
    ON dispatch_telemetry(repo);
CREATE INDEX IF NOT EXISTS idx_dispatch_telemetry_task_class
    ON dispatch_telemetry(task_class);
CREATE INDEX IF NOT EXISTS idx_dispatch_telemetry_model
    ON dispatch_telemetry(model_provider, model);
CREATE INDEX IF NOT EXISTS idx_dispatch_telemetry_pr
    ON dispatch_telemetry(repo, pr_number);
CREATE INDEX IF NOT EXISTS idx_dispatch_telemetry_accepted
    ON dispatch_telemetry(accepted_outcome_id);
"""


def _lookup(data, path):
    cur = data
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur.get(part)
    return cur


def _first(data, paths):
    for path in paths:
        value = _lookup(data, path if isinstance(path, tuple) else (path,))
        if value not in (None, ""):
            return value
    return None


def _pid(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def normalize_dispatch_telemetry(response):
    """Return one stable telemetry dict from current and older gateway shapes."""
    response = response if isinstance(response, dict) else {}
    telemetry = {
        "dispatch_id": _first(response, (
            "dispatch_id", "id", "request_id",
            ("dispatch", "id"), ("data", "dispatch_id"), ("data", "id"),
        )),
        "session_id": _first(response, (
            "session_id", "session", "agent_session_id",
            ("session", "id"), ("agent", "session_id"), ("data", "session_id"),
        )),
        "pid": _pid(_first(response, (
            "pid", "agent_pid",
            ("process", "pid"), ("agent", "pid"), ("worker", "pid"), ("data", "pid"),
        ))),
        "log_path": _first(response, (
            "log_path", "agent_log", "log_file",
            ("paths", "log"), ("files", "log"), ("agent", "log_path"), ("data", "log_path"),
        )),
        "transcript_path": _first(response, (
            "transcript_path", "transcript_file",
            ("paths", "transcript"), ("files", "transcript"),
            ("agent", "transcript_path"), ("data", "transcript_path"),
        )),
        "status_url": _first(response, (
            "status_url",
            ("urls", "status"), ("links", "status"), ("data", "status_url"),
        )),
        "status_path": _first(response, (
            "status_path",
            ("paths", "status"), ("files", "status"), ("data", "status_path"),
        )),
        "session_path": _first(response, (
            "session_path",
            ("paths", "session"), ("files", "session"), ("data", "session_path"),
        )),
        "branch": _first(response, (
            "branch", "branch_name", "head_ref",
            ("git", "branch"), ("agent", "branch"), ("data", "branch"),
        )),
        "worktree": _first(response, (
            "worktree", "worktree_path", "local_path",
            ("paths", "worktree"), ("agent", "worktree"), ("data", "worktree"),
        )),
    }
    telemetry["agent_pid"] = telemetry["pid"]

    missing = [
        key for key in ("dispatch_id", "session_id", "pid", "log_path")
        if telemetry.get(key) in (None, "")
    ]
    telemetry["telemetry_missing"] = missing
    telemetry["liveness_reliable"] = bool(telemetry.get("pid"))
    return telemetry


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)


def _connect(db_path: Path | str = TELEMETRY_DB) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript(SCHEMA)
    return db


def _source(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value.get("kind"):
        raise ValueError(f"{field}.source must identify its kind")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def normalize_terminal_telemetry(value: Any) -> dict[str, Any]:
    """Validate exact terminal telemetry without filling or estimating values."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("telemetry must be an object")

    usage = value.get("usage") or {
        "status": NOT_AVAILABLE,
        "source": {"kind": "not_exposed"},
    }
    if not isinstance(usage, dict):
        raise ValueError("telemetry.usage must be an object")
    usage_status = usage.get("status", NOT_AVAILABLE)
    if usage_status not in ("available", NOT_AVAILABLE):
        raise ValueError("telemetry.usage.status must be available or not_available")
    usage_source = _source(usage.get("source"), "telemetry.usage")
    if usage_status == "available" and usage_source.get("kind") != "provider_response":
        raise ValueError(
            "available telemetry.usage source must be an exact provider_response"
        )
    normalized_usage: dict[str, Any] = {
        "status": usage_status,
        "source": usage_source,
    }
    for field in USAGE_FIELDS:
        field_value = usage.get(field)
        if field_value is not None:
            if usage_status != "available":
                raise ValueError(f"telemetry.usage.{field} requires available status")
            normalized_usage[field] = _nonnegative_int(
                field_value, f"telemetry.usage.{field}"
            )
        else:
            normalized_usage[field] = None
    if usage_status == "available" and not any(
        normalized_usage[field] is not None for field in USAGE_FIELDS
    ):
        raise ValueError("available telemetry.usage must expose at least one exact field")

    cost = value.get("cost") or {
        "status": NOT_AVAILABLE,
        "source": {"kind": "not_exposed"},
    }
    if not isinstance(cost, dict):
        raise ValueError("telemetry.cost must be an object")
    cost_status = cost.get("status", NOT_AVAILABLE)
    if cost_status not in ("available", NOT_AVAILABLE):
        raise ValueError("telemetry.cost.status must be available or not_available")
    cost_source = _source(cost.get("source"), "telemetry.cost")
    normalized_cost: dict[str, Any] = {
        "status": cost_status,
        "source": cost_source,
        "amount_micros": None,
        "currency": None,
        "price_version": None,
        "formula": None,
        "price_inputs": {},
    }
    if cost_status == "available":
        if cost_source.get("kind") not in (
            "provider_reported", "provider_pricing_formula"
        ):
            raise ValueError(
                "available telemetry.cost source must be provider_reported "
                "or provider_pricing_formula"
            )
        normalized_cost["amount_micros"] = _nonnegative_int(
            cost.get("amount_micros"), "telemetry.cost.amount_micros"
        )
        currency = cost.get("currency")
        if not isinstance(currency, str) or len(currency.strip()) != 3:
            raise ValueError("telemetry.cost.currency must be a three-letter code")
        normalized_cost["currency"] = currency.strip().upper()
        for field in ("price_version", "formula"):
            field_value = cost.get(field)
            if not isinstance(field_value, str) or not field_value.strip():
                raise ValueError(f"telemetry.cost.{field} is required")
            normalized_cost[field] = field_value.strip()
        price_inputs = cost.get("price_inputs", {})
        if not isinstance(price_inputs, dict):
            raise ValueError("telemetry.cost.price_inputs must be an object")
        if (
            cost_source.get("kind") == "provider_pricing_formula"
            and not price_inputs
        ):
            raise ValueError(
                "provider_pricing_formula cost requires auditable price_inputs"
            )
        normalized_cost["price_inputs"] = price_inputs
    elif any(cost.get(field) not in (None, "", {}) for field in (
        "amount_micros", "currency", "price_version", "formula", "price_inputs"
    )):
        raise ValueError("not_available telemetry.cost cannot contain cost values")

    accepted = value.get("accepted_outcome") or {
        "status": NOT_AVAILABLE,
        "source": {"kind": "not_exposed"},
    }
    if not isinstance(accepted, dict):
        raise ValueError("telemetry.accepted_outcome must be an object")
    accepted_status = accepted.get("status", NOT_AVAILABLE)
    if accepted_status not in ("accepted", "not_accepted", NOT_AVAILABLE):
        raise ValueError(
            "telemetry.accepted_outcome.status must be accepted, "
            "not_accepted, or not_available"
        )
    accepted_source = _source(
        accepted.get("source"), "telemetry.accepted_outcome"
    )
    accepted_id = accepted.get("id")
    if accepted_status == "accepted":
        if not isinstance(accepted_id, str) or not accepted_id.strip():
            raise ValueError("accepted telemetry outcome requires an id")
        accepted_id = accepted_id.strip()
    elif accepted_id is not None:
        raise ValueError("only accepted telemetry outcomes may contain an id")

    return {
        "usage": normalized_usage,
        "cost": normalized_cost,
        "accepted_outcome": {
            "status": accepted_status,
            "id": accepted_id,
            "source": accepted_source,
        },
    }


def record_dispatch_start(
    *,
    dispatch_id: str,
    item_id: str,
    repo: str | None,
    task_class: str | None,
    model_provider: str | None,
    model: str | None,
    started_at: str,
    db_path: Path | str = TELEMETRY_DB,
) -> None:
    """Persist deterministic dispatch dimensions before the worker runs."""
    if not dispatch_id or not item_id or not started_at:
        raise ValueError("dispatch_id, item_id, and started_at are required")
    _parse_timestamp(started_at, "started_at")
    with _connect(db_path) as db:
        db.execute(
            """
            INSERT INTO dispatch_telemetry
              (dispatch_id, item_id, repo, task_class, model_provider, model,
               started_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dispatch_id) DO UPDATE SET
              item_id=excluded.item_id,
              repo=COALESCE(excluded.repo, dispatch_telemetry.repo),
              task_class=COALESCE(excluded.task_class, dispatch_telemetry.task_class),
              model_provider=COALESCE(excluded.model_provider, dispatch_telemetry.model_provider),
              model=COALESCE(excluded.model, dispatch_telemetry.model),
              started_at=COALESCE(dispatch_telemetry.started_at, excluded.started_at),
              updated_at=excluded.updated_at
            """,
            (
                dispatch_id, item_id, repo, task_class, model_provider, model,
                started_at, _now_iso(),
            ),
        )


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed


def record_terminal_result(
    result: dict[str, Any],
    *,
    dispatch_context: dict[str, Any] | None = None,
    db_path: Path | str = TELEMETRY_DB,
) -> None:
    """Attach exact terminal metrics to the matching dispatch, idempotently."""
    context = dispatch_context or {}
    dispatch_id = result.get("dispatch_id")
    item_id = result.get("item_id")
    if not dispatch_id or not item_id:
        raise ValueError("terminal telemetry requires dispatch_id and item_id")
    terminal = normalize_terminal_telemetry(result.get("telemetry"))
    finished_at = result.get("occurred_at")
    finished = _parse_timestamp(finished_at, "occurred_at")
    started_at = (
        context.get("agent_started_at")
        or context.get("started_at")
    )

    with _connect(db_path) as db:
        existing = db.execute(
            "SELECT * FROM dispatch_telemetry WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
        if (
            existing
            and existing["finished_at"]
            and existing["status"] in ("completed", "failed")
        ):
            return
        if existing and existing["started_at"]:
            started_at = existing["started_at"]
        duration_ms = None
        duration_status = NOT_AVAILABLE
        duration_source = {"kind": "dispatch_timestamps"}
        if started_at:
            started = _parse_timestamp(started_at, "started_at")
            elapsed_ms = int((finished - started).total_seconds() * 1000)
            if elapsed_ms >= 0:
                duration_ms = elapsed_ms
                duration_status = "available"
            else:
                duration_source["reason"] = "finished_before_started"
        else:
            duration_source["reason"] = "started_at_not_available"

        usage = terminal["usage"]
        cost = terminal["cost"]
        accepted = terminal["accepted_outcome"]
        db.execute(
            """
            INSERT INTO dispatch_telemetry
              (dispatch_id, item_id, repo, task_class, model_provider, model,
               status, pr_number, accepted_outcome_id,
               accepted_outcome_status, accepted_outcome_source_json,
               started_at, finished_at, duration_ms, duration_status,
               duration_source_json, usage_status, input_tokens, output_tokens,
               cached_input_tokens, reasoning_tokens, total_tokens, api_calls,
               usage_source_json, cost_status, cost_micros, cost_currency,
               cost_source_json, cost_price_version, cost_formula,
               cost_price_inputs_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dispatch_id) DO UPDATE SET
              status=excluded.status,
              pr_number=excluded.pr_number,
              accepted_outcome_id=excluded.accepted_outcome_id,
              accepted_outcome_status=excluded.accepted_outcome_status,
              accepted_outcome_source_json=excluded.accepted_outcome_source_json,
              finished_at=excluded.finished_at,
              duration_ms=excluded.duration_ms,
              duration_status=excluded.duration_status,
              duration_source_json=excluded.duration_source_json,
              usage_status=excluded.usage_status,
              input_tokens=excluded.input_tokens,
              output_tokens=excluded.output_tokens,
              cached_input_tokens=excluded.cached_input_tokens,
              reasoning_tokens=excluded.reasoning_tokens,
              total_tokens=excluded.total_tokens,
              api_calls=excluded.api_calls,
              usage_source_json=excluded.usage_source_json,
              cost_status=excluded.cost_status,
              cost_micros=excluded.cost_micros,
              cost_currency=excluded.cost_currency,
              cost_source_json=excluded.cost_source_json,
              cost_price_version=excluded.cost_price_version,
              cost_formula=excluded.cost_formula,
              cost_price_inputs_json=excluded.cost_price_inputs_json,
              updated_at=excluded.updated_at
            """,
            (
                dispatch_id,
                item_id,
                result.get("repo") or context.get("repo"),
                context.get("task_class") or context.get("agent_prompt"),
                context.get("model_provider"),
                context.get("model"),
                result.get("status") or "unknown",
                result.get("pr_number"),
                accepted.get("id"),
                accepted["status"],
                _json(accepted["source"]),
                started_at,
                finished_at,
                duration_ms,
                duration_status,
                _json(duration_source),
                usage["status"],
                usage["input_tokens"],
                usage["output_tokens"],
                usage["cached_input_tokens"],
                usage["reasoning_tokens"],
                usage["total_tokens"],
                usage["api_calls"],
                _json(usage["source"]),
                cost["status"],
                cost["amount_micros"],
                cost["currency"],
                _json(cost["source"]),
                cost["price_version"],
                cost["formula"],
                _json(cost["price_inputs"]),
                _now_iso(),
            ),
        )


def _row_dict(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key in (
        "accepted_outcome_source_json", "duration_source_json",
        "usage_source_json", "cost_source_json", "cost_price_inputs_json",
    ):
        output_key = key.removesuffix("_json")
        try:
            data[output_key] = json.loads(data.pop(key) or "{}")
        except (TypeError, json.JSONDecodeError):
            data[output_key] = {"kind": "invalid_stored_metadata"}
    return data


def get_dispatch(
    dispatch_id: str,
    *,
    db_path: Path | str = TELEMETRY_DB,
) -> dict[str, Any] | None:
    with _connect(db_path) as db:
        row = db.execute(
            "SELECT * FROM dispatch_telemetry WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
    return _row_dict(row)


def list_dispatches(*, db_path: Path | str = TELEMETRY_DB) -> list[dict[str, Any]]:
    with _connect(db_path) as db:
        rows = db.execute(
            "SELECT * FROM dispatch_telemetry ORDER BY started_at, dispatch_id"
        ).fetchall()
    return [_row_dict(row) for row in rows if row is not None]


def serialize_dispatch(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Render unavailable numeric metrics explicitly for internal consumers."""
    if row is None:
        return None
    rendered = dict(row)
    for field in ("duration_ms",) + USAGE_FIELDS + ("cost_micros",):
        if rendered.get(field) is None:
            rendered[field] = NOT_AVAILABLE
    return rendered


def _metric(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    values = [row[field] for row in rows if row.get(field) is not None]
    total = len(rows)
    available = len(values)
    status = (
        NOT_AVAILABLE if available == 0
        else "complete" if available == total
        else "partial"
    )
    return {
        "status": status,
        "value": sum(values) if values else NOT_AVAILABLE,
        "available_count": available,
        "not_available_count": total - available,
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "dispatch_count": len(rows),
        "duration": _metric(rows, "duration_ms"),
        **{field: _metric(rows, field) for field in USAGE_FIELDS},
        "cost": _cost_metric(rows),
    }


def _cost_metric(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_currency: dict[str, int] = defaultdict(int)
    available = 0
    for row in rows:
        if row.get("cost_micros") is None or not row.get("cost_currency"):
            continue
        available += 1
        by_currency[str(row["cost_currency"])] += int(row["cost_micros"])
    total = len(rows)
    status = (
        NOT_AVAILABLE if available == 0
        else "complete" if available == total
        else "partial"
    )
    return {
        "status": status,
        "by_currency_micros": dict(sorted(by_currency.items())),
        "available_count": available,
        "not_available_count": total - available,
    }


def _group(
    rows: list[dict[str, Any]],
    key,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        group_key = key(row)
        if group_key:
            groups[str(group_key)].append(row)
    return {name: _aggregate(group_rows) for name, group_rows in sorted(groups.items())}


def build_internal_report(
    *,
    db_path: Path | str = TELEMETRY_DB,
) -> dict[str, Any]:
    """Return cost-bearing internal aggregates over independent dimensions."""
    rows = list_dispatches(db_path=db_path)
    terminal_rows = [row for row in rows if row.get("finished_at")]
    return {
        "summary": _aggregate(terminal_rows),
        "by_dispatch": [serialize_dispatch(row) for row in rows],
        "by_repo": _group(terminal_rows, lambda row: row.get("repo")),
        "by_task_class": _group(
            terminal_rows, lambda row: row.get("task_class")
        ),
        "by_model": _group(
            terminal_rows,
            lambda row: (
                f"{row.get('model_provider')}/{row.get('model')}"
                if row.get("model_provider") and row.get("model")
                else None
            ),
        ),
        "by_pr": _group(
            terminal_rows,
            lambda row: (
                f"{row.get('repo')}#{row.get('pr_number')}"
                if row.get("repo") and row.get("pr_number")
                else None
            ),
        ),
        "by_accepted_outcome": _group(
            terminal_rows,
            lambda row: (
                row.get("accepted_outcome_id")
                if row.get("accepted_outcome_status") == "accepted"
                else None
            ),
        ),
    }


def build_public_completeness_report(
    *,
    db_path: Path | str = TELEMETRY_DB,
) -> dict[str, Any]:
    """Expose completeness without client-sensitive cost values or basis."""
    rows = [row for row in list_dispatches(db_path=db_path) if row.get("finished_at")]
    return {
        "dispatch_count": len(rows),
        "duration_complete_count": sum(
            row.get("duration_status") == "available" for row in rows
        ),
        "token_complete_count": sum(
            all(row.get(field) is not None for field in TOKEN_FIELDS) for row in rows
        ),
        "api_call_complete_count": sum(
            row.get("api_calls") is not None for row in rows
        ),
        "cost_complete_count": sum(
            row.get("cost_status") == "available"
            and row.get("cost_micros") is not None
            for row in rows
        ),
    }
