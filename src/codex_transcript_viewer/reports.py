"""Report builders over immutable breakdown, spans, and metrics documents."""

from __future__ import annotations

import csv
import io
from copy import deepcopy
from typing import Any


_TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
_CONTEXT_FIELDS = ("arguments_size", "content_size", "input_size", "message_size", "output_size", "summary_size")


def _csv(rows: list[dict[str, Any]], fields: list[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _number(value: Any) -> int | float | str:
    return value if isinstance(value, (int, float)) else ""


def _sessions_table_metrics(metrics: dict[str, Any], *, tree: bool = False) -> dict[str, Any]:
    """Select the stable, report-ready aggregate subset from full metrics."""
    context = metrics.get("context_material") if isinstance(metrics.get("context_material"), dict) else {}
    events = metrics.get("events") if isinstance(metrics.get("events"), dict) else {}
    reported = metrics.get("reported_token_usage") if isinstance(metrics.get("reported_token_usage"), dict) else {}
    result = {
        "events": {"native_by_kind": deepcopy(events.get("native_by_kind", {}))},
        "context_material": {field: context.get(field, 0) for field in _CONTEXT_FIELDS},
        "total_tool_time_ms": metrics.get("total_tool_time_ms", 0),
        "total_reasoning_time_ms": metrics.get("total_reasoning_time_ms", 0),
    }
    if tree:
        result["reported_token_usage"] = {"sum_session_cumulative": deepcopy(reported.get("sum_session_cumulative", {}))}
        result["events"]["native_total"] = events.get("native_total", 0)
        result["wall_clock"] = deepcopy(metrics.get("wall_clock", {}))
        result["session_count"] = metrics.get("session_count", 0)
    else:
        result["reported_token_usage"] = {"last_cumulative": deepcopy(reported.get("last_cumulative", {}))}
        result["rate_limits"] = deepcopy(metrics.get("rate_limits", {"primary": {"used_percent": None}}))
    return result


def build_sessions_table_json(metrics_document: dict[str, Any]) -> dict[str, Any]:
    """Project a compact sessions-and-tree report from the metrics document."""
    sessions = []
    for session in metrics_document.get("sessions", []):
        if not isinstance(session, dict):
            continue
        metrics = session.get("metrics") if isinstance(session.get("metrics"), dict) else {}
        sessions.append({
            "session_id": session.get("session_id", ""),
            "parent_session_id": session.get("parent_session_id", ""),
            "agent_path": session.get("agent_path", ""),
            "thread_source": session.get("thread_source", ""),
            **_sessions_table_metrics(metrics),
        })
    tree_metrics = metrics_document.get("tree_metrics") if isinstance(metrics_document.get("tree_metrics"), dict) else {}
    return {
        "report_version": 1,
        "report_kind": "sessions_table",
        "source": deepcopy(metrics_document.get("source", {})),
        "tree": _sessions_table_metrics(tree_metrics, tree=True),
        "sessions": sessions,
    }


def build_session_events_table_csv(session: dict[str, Any], spans: dict[str, Any], until_ms: int | None = None) -> str:
    """Export the same logical event rows as the trace table for one session."""
    fields = [
        "timestamp", "duration_ms", "span_kind", "event_kind", "operation",
        "payload_input_bytes", "payload_output_bytes", "payload_cumulative_bytes",
        "last_input_tokens", "last_cached_input_tokens", "last_cache_write_input_tokens", "last_output_tokens", "last_reasoning_output_tokens",
        "total_input_tokens", "total_cached_input_tokens", "total_cache_write_input_tokens", "total_output_tokens", "total_reasoning_output_tokens",
    ]
    all_spans = [span for span in spans.get("spans", []) if isinstance(span, dict)]
    tools = {span.get("start_event_id"): span for span in all_spans if span.get("kind") == "tool"}
    reasoning = {span.get("end_event_id"): span for span in all_spans if span.get("kind") == "reasoning"}
    linked_outputs = {event_id for span in tools.values() for event_id in span.get("event_ids", [])[1:]}
    events = [
        event for event in session.get("events", [])
        if isinstance(event, dict) and (until_ms is None or not isinstance(event.get("timestamp_ms"), int) or event["timestamp_ms"] <= until_ms)
    ]
    cumulative = 0
    cumulative_by_id: dict[str, int] = {}
    for event in events:
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        size = details.get("payload_size") if isinstance(details.get("payload_size"), dict) else {}
        cumulative += int(size.get("serialized_json_utf8_bytes") or 0)
        cumulative_by_id[str(event.get("event_id"))] = cumulative

    rows: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event.get("event_id"))
        if event_id in linked_outputs:
            continue
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        tool = tools.get(event_id)
        reasoning_span = reasoning.get(event_id)
        span = tool or reasoning_span
        span_kind = str(span.get("kind")) if span else "event"
        end_id = str(span.get("end_event_id")) if span else event_id
        clipped = bool(
            span and until_ms is not None and isinstance(span.get("start_ms"), int) and isinstance(span.get("end_ms"), int)
            and span["start_ms"] <= until_ms < span["end_ms"]
        )
        duration = (until_ms - span["start_ms"]) if clipped else (span.get("duration_ms") if span else event.get("duration", {}).get("observed_ms"))
        operation = str(event.get("kind") or event.get("payload_type") or event.get("outer_type") or "event")
        if tool:
            attrs = tool.get("attributes") if isinstance(tool.get("attributes"), dict) else {}
            labels = [str(attrs.get("name") or "tool")]
            for call in attrs.get("nested_calls", []):
                if isinstance(call, dict):
                    labels.append(str(call.get("command_label") or call.get("command_name") or call.get("tool") or ""))
            operation = " → ".join(label for label in labels if label)
        elif event.get("kind") == "sub_agent_activity":
            operation = " · ".join(str(details[key]) for key in ("agent_path", "kind") if details.get(key)) or operation
        elif event.get("kind") in {"agent_message", "message"}:
            operation = " → ".join(str(details[key]) for key in ("author", "recipient") if details.get(key)) or operation
        info = details.get("info") if isinstance(details.get("info"), dict) else {}
        last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
        total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
        input_size = details.get("input_size") if isinstance(details.get("input_size"), dict) else details.get("arguments_size", {})
        output_size = details.get("output_size") if isinstance(details.get("output_size"), dict) else {}
        attrs = tool.get("attributes", {}) if tool else {}
        rows.append({
            "timestamp": event.get("timestamp", ""), "duration_ms": _number(duration), "span_kind": span_kind, "event_kind": event.get("kind", ""), "operation": operation,
            "payload_input_bytes": _number(attrs.get("input_bytes") if tool else input_size.get("serialized_json_utf8_bytes")),
            "payload_output_bytes": _number(None if tool and clipped else (attrs.get("output_bytes") if tool else output_size.get("serialized_json_utf8_bytes"))),
            "payload_cumulative_bytes": cumulative_by_id.get(event_id) if clipped else cumulative_by_id.get(end_id, cumulative_by_id.get(event_id, "")),
            **{f"last_{key}": _number(last.get(key)) for key in _TOKEN_KEYS[:-1]},
            **{f"total_{key}": _number(total.get(key)) for key in _TOKEN_KEYS[:-1]},
        })
    return _csv(rows, fields)
