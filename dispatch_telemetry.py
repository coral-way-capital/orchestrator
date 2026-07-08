#!/usr/bin/env python3
"""Helpers for normalizing Hermes gateway dispatch telemetry."""


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
    }
    telemetry["agent_pid"] = telemetry["pid"]

    missing = [
        key for key in ("dispatch_id", "session_id", "pid", "log_path")
        if telemetry.get(key) in (None, "")
    ]
    telemetry["telemetry_missing"] = missing
    telemetry["liveness_reliable"] = bool(telemetry.get("pid"))
    return telemetry
