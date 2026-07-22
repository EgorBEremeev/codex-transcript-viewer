"""Derived session and tree metrics for a raw breakdown dataset."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any

from .spans import breakdown_sha256


METRICS_ANALYSIS_VERSION = 1
_CONTENT_FIELDS = ("input_size", "arguments_size", "output_size", "content_size", "message_size", "summary_size")


def _add_numeric(target: dict[str, Any], value: Any) -> None:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if isinstance(item, dict):
            nested = target.setdefault(key, {})
            if isinstance(nested, dict):
                _add_numeric(nested, item)
        elif isinstance(item, (int, float)):
            target[key] = target.get(key, 0) + item


def _context_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        origin: {"text_chars": 0, "utf8_bytes": 0, "serialized_json_chars": 0, "encrypted_chars": 0}
        for origin in ("native", "inherited", "unknown")
    }
    by_kind: Counter[str] = Counter()
    for event in events:
        origin = event.get("record_origin") if event.get("record_origin") in totals else "unknown"
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        for label in _CONTENT_FIELDS:
            size = details.get(label)
            if not isinstance(size, dict):
                continue
            totals[origin]["text_chars"] += int(size.get("text_chars") or 0)
            totals[origin]["utf8_bytes"] += int(size.get("utf8_bytes") or 0)
            totals[origin]["serialized_json_chars"] += int(size.get("serialized_json_chars") or 0)
            totals[origin]["encrypted_chars"] += sum(
                int(block.get("encrypted_content_chars") or 0)
                for block in size.get("blocks", [])
                if isinstance(block, dict)
            )
            by_kind[label] += int(size.get("text_chars") or 0)
        totals[origin]["encrypted_chars"] += int(details.get("encrypted_content_chars") or 0)
    flat = dict(sorted(by_kind.items()))
    return {**totals, "by_content_kind": flat, **flat}


def _rate_limits(events: list[dict[str, Any]]) -> dict[str, Any]:
    used = []
    for event in events:
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        rate_limits = details.get("rate_limits") if isinstance(details.get("rate_limits"), dict) else {}
        primary = rate_limits.get("primary") if isinstance(rate_limits.get("primary"), dict) else {}
        value = primary.get("used_percent")
        if isinstance(value, (int, float)):
            used.append(value)
    return {"primary": {"used_percent": used[-1] - used[0] if len(used) >= 2 else None}}


def _span_total(spans: list[dict[str, Any]], session_id: str, kind: str, native_ids: set[str]) -> int | float:
    total: int | float = 0
    for span in spans:
        if span.get("kind") != kind or span.get("session_id") != session_id:
            continue
        if span.get("end_event_id") not in native_ids:
            continue
        duration = span.get("duration_ms")
        if isinstance(duration, (int, float)):
            total += duration
    return total


def _session_metrics(session: dict[str, Any], spans: list[dict[str, Any]]) -> dict[str, Any]:
    events = [event for event in session.get("events", []) if isinstance(event, dict)]
    native = [event for event in events if event.get("record_origin") == "native"]
    native_ids = {str(event.get("event_id")) for event in native}
    tokens = [event for event in native if event.get("payload_type") == "token_count"]
    cumulative = {}
    if tokens:
        details = tokens[-1].get("details") if isinstance(tokens[-1].get("details"), dict) else {}
        info = details.get("info") if isinstance(details.get("info"), dict) else {}
        cumulative = deepcopy(info.get("total_token_usage") or {})
    return {
        "events": {"native_by_kind": dict(Counter(str(event.get("kind") or "unknown") for event in native))},
        "context_material": _context_summary(events),
        "reported_token_usage": {
            "snapshot_count": len(tokens),
            "last_cumulative": cumulative,
            "resets": [str(event.get("event_id")) for event in tokens if isinstance(event.get("details"), dict) and event["details"].get("reset_detected")],
        },
        "rate_limits": _rate_limits(tokens),
        "total_tool_time_ms": _span_total(spans, str(session.get("session_id") or ""), "tool", native_ids),
        "total_reasoning_time_ms": _span_total(spans, str(session.get("session_id") or ""), "reasoning", native_ids),
    }


def _tree_metrics(breakdown: dict[str, Any], sessions: list[dict[str, Any]]) -> dict[str, Any]:
    timeline = [item for item in breakdown.get("timeline", []) if isinstance(item, dict) and isinstance(item.get("timestamp_ms"), int)]
    timeline.sort(key=lambda item: (item["timestamp_ms"], item.get("line", 0), item.get("session_id", "")))
    event_counts: Counter[str] = Counter()
    token_usage: dict[str, Any] = {}
    context: dict[str, Any] = {}
    total_tool_time: int | float = 0
    total_reasoning_time: int | float = 0
    for session in sessions:
        metrics = session["metrics"]
        event_counts.update(metrics["events"]["native_by_kind"])
        _add_numeric(token_usage, metrics["reported_token_usage"]["last_cumulative"])
        _add_numeric(context, metrics["context_material"])
        total_tool_time += metrics["total_tool_time_ms"]
        total_reasoning_time += metrics["total_reasoning_time_ms"]
    return {
        "wall_clock": {
            "started_at": timeline[0].get("timestamp", "") if timeline else "",
            "ended_at": timeline[-1].get("timestamp", "") if timeline else "",
            "duration_ms": timeline[-1]["timestamp_ms"] - timeline[0]["timestamp_ms"] if timeline else None,
        },
        "session_count": len(sessions),
        "events": {"native_by_kind": dict(event_counts), "native_total": sum(event_counts.values())},
        "reported_token_usage": {"sum_session_cumulative": token_usage},
        "context_material": context,
        "total_tool_time_ms": total_tool_time,
        "total_reasoning_time_ms": total_reasoning_time,
    }


def build_sessions_metrics(breakdown: dict[str, Any], spans: dict[str, Any]) -> dict[str, Any]:
    """Build a compact metrics document linked to one immutable breakdown."""
    root_id = str(breakdown.get("root_session_id") or "")
    if not root_id:
        raise ValueError("breakdown root_session_id is missing")
    span_list = [span for span in spans.get("spans", []) if isinstance(span, dict)]
    sessions = []
    for raw in breakdown.get("sessions", []):
        if not isinstance(raw, dict):
            continue
        meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
        sessions.append({
            "session_id": str(raw.get("session_id") or ""),
            "parent_session_id": str(raw.get("parent_session_id") or ""),
            "agent_path": str(raw.get("agent_path") or ""),
            "thread_source": str(meta.get("thread_source") or ""),
            "metrics": _session_metrics(raw, span_list),
        })
    return {
        "analysis_version": METRICS_ANALYSIS_VERSION,
        "source": {
            "root_session_id": root_id,
            "breakdown_schema_version": breakdown.get("schema_version"),
            "breakdown_sha256": breakdown_sha256(breakdown),
        },
        "tree_metrics": _tree_metrics(breakdown, sessions),
        "sessions": sessions,
    }
