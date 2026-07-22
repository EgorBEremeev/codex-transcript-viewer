"""Build derived trace spans from an immutable breakdown dataset."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from copy import deepcopy
from typing import Any


SPAN_ANALYSIS_VERSION = 1


def breakdown_sha256(data: dict[str, Any]) -> str:
    """Return a stable digest for a parsed breakdown document."""
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _event_bounds(events: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    dated = [event for event in events if isinstance(event.get("timestamp_ms"), int)]
    if not dated:
        return None, None
    return min(dated, key=lambda event: (event["timestamp_ms"], event.get("line", 0))), max(
        dated, key=lambda event: (event["timestamp_ms"], event.get("line", 0))
    )


def _duration(start: dict[str, Any] | None, end: dict[str, Any] | None, preferred: Any = None) -> int | float | None:
    if isinstance(preferred, (int, float)):
        return preferred
    if start and end and isinstance(start.get("timestamp_ms"), int) and isinstance(end.get("timestamp_ms"), int):
        return max(0, end["timestamp_ms"] - start["timestamp_ms"])
    return None


def _span(
    span_id: str,
    kind: str,
    *,
    parent_span_id: str | None,
    session_id: str | None,
    turn_id: str | None,
    event_ids: list[str],
    start: dict[str, Any] | None,
    end: dict[str, Any] | None,
    duration_ms: int | float | None = None,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "span_id": span_id,
        "kind": kind,
        "parent_span_id": parent_span_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "start_event_id": start.get("event_id") if start else None,
        "end_event_id": end.get("event_id") if end else None,
        "event_ids": event_ids,
        "start_at": start.get("timestamp") if start else None,
        "start_ms": start.get("timestamp_ms") if start else None,
        "end_at": end.get("timestamp") if end else None,
        "end_ms": end.get("timestamp_ms") if end else None,
        "duration_ms": _duration(start, end, duration_ms),
        "attributes": attributes or {},
    }


def _payload_bytes(event: dict[str, Any]) -> int:
    size = event.get("details", {}).get("payload_size", {})
    value = size.get("serialized_json_utf8_bytes") if isinstance(size, dict) else None
    return int(value) if isinstance(value, int) else 0


def _content_bytes(event: dict[str, Any], key: str) -> int:
    size = event.get("details", {}).get(key, {})
    value = size.get("serialized_json_utf8_bytes") if isinstance(size, dict) else None
    return int(value) if isinstance(value, int) else 0


def _tool_attributes(call: dict[str, Any], outputs: list[dict[str, Any]]) -> dict[str, Any]:
    details = call.get("details", {})
    nested = details.get("nested_calls") if isinstance(details.get("nested_calls"), list) else []
    nested_projection = [
        {
            "tool": item.get("tool"),
            "command_name": item.get("command_name"),
            "command_label": item.get("command_label"),
            "command_arguments": item.get("command_arguments"),
            "command_operation": item.get("command_operation"),
            "extraction": deepcopy(item.get("extraction")),
        }
        for item in nested
        if isinstance(item, dict)
    ]
    return {
        "name": details.get("name"),
        "namespace": details.get("namespace"),
        "call_id": details.get("call_id"),
        "status": details.get("status"),
        "nested_calls": nested_projection,
        "payload_input_bytes": _payload_bytes(call),
        "payload_output_bytes": sum(_payload_bytes(event) for event in outputs),
        "payload_total_bytes": _payload_bytes(call) + sum(_payload_bytes(event) for event in outputs),
        "input_bytes": _content_bytes(call, "input_size") or _content_bytes(call, "arguments_size"),
        "output_bytes": sum(_content_bytes(event, "output_size") for event in outputs),
    }


def _completed_turns(events: list[dict[str, Any]], legacy: Any) -> dict[str, dict[str, Any]]:
    """Derive completed turn boundaries from raw events (legacy data may carry them)."""
    result = {
        str(turn.get("turn_id")): turn
        for turn in legacy if isinstance(turn, dict) and isinstance(turn.get("turn_id"), str)
    } if isinstance(legacy, list) else {}
    starts: dict[str, dict[str, Any]] = {}
    for event in events:
        turn_id = event.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            continue
        if event.get("payload_type") == "task_started":
            starts[turn_id] = event
        elif event.get("payload_type") in {"task_complete", "turn_aborted"} and turn_id in starts:
            start = starts.pop(turn_id)
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            result[turn_id] = {
                "turn_id": turn_id,
                "start_event_id": start.get("event_id"),
                "end_event_id": event.get("event_id"),
                "duration_ms": event.get("duration", {}).get("reported_ms") or details.get("duration_ms"),
                "time_to_first_token_ms": details.get("time_to_first_token_ms"),
                "outcome": "completed" if event.get("payload_type") == "task_complete" else "aborted",
            }
    return result


def build_spans(breakdown: dict[str, Any]) -> dict[str, Any]:
    """Create a compact, reproducible span projection without copying raw events."""
    if not isinstance(breakdown, dict) or not isinstance(breakdown.get("sessions"), list):
        raise ValueError("input is not a breakdown dataset with sessions")
    root_id = breakdown.get("root_session_id")
    if not isinstance(root_id, str) or not root_id:
        raise ValueError("breakdown root_session_id is missing")

    spans: list[dict[str, Any]] = []
    warnings: list[str] = []
    event_to_span: dict[str, list[str]] = defaultdict(list)
    events_by_id: dict[str, dict[str, Any]] = {}
    sessions_by_id: dict[str, dict[str, Any]] = {}
    events_by_session_turn: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for session in breakdown["sessions"]:
        if not isinstance(session, dict):
            continue
        session_id = session.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            warnings.append("session without session_id was skipped")
            continue
        for event in session.get("events", []):
            if not isinstance(event, dict) or not isinstance(event.get("event_id"), str):
                continue
            event_id = event["event_id"]
            if event_id in events_by_id:
                raise ValueError(f"duplicate event_id in breakdown: {event_id}")
            events_by_id[event_id] = event
            turn_id = event.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                events_by_session_turn[(session_id, turn_id)].append(event)
        sessions_by_id[session_id] = session

    all_events = list(events_by_id.values())
    task_start, task_end = _event_bounds(all_events)
    task = _span(
        f"task:{root_id}", "task", parent_span_id=None, session_id=None, turn_id=None,
        event_ids=[], start=task_start, end=task_end,
        attributes={"root_session_id": root_id},
    )
    spans.append(task)

    for session_id, session in sessions_by_id.items():
        events = [event for event in session.get("events", []) if isinstance(event, dict) and event.get("event_id") in events_by_id]
        start, end = _event_bounds(events)
        parent_id = session.get("parent_session_id")
        parent_span_id = (
            f"session:{parent_id}"
            if isinstance(parent_id, str) and parent_id in sessions_by_id
            else task["span_id"]
        )
        meta = session.get("meta", {}) if isinstance(session.get("meta"), dict) else {}
        span = _span(
            f"session:{session_id}", "session", parent_span_id=parent_span_id,
            session_id=session_id, turn_id=None,
            event_ids=[event["event_id"] for event in events], start=start, end=end,
            attributes={
                "agent_path": session.get("agent_path") or "",
                "agent_role": meta.get("agent_role"),
                "thread_source": meta.get("thread_source"),
                "record_scope": deepcopy(session.get("record_scope") or {}),
            },
        )
        spans.append(span)
        for event_id in span["event_ids"]:
            event_to_span[event_id].append(span["span_id"])

        completed_turns = _completed_turns(events, session.get("turns"))
        known_turn_ids = set(completed_turns) | {turn_id for current_session, turn_id in events_by_session_turn if current_session == session_id}
        for turn_id in sorted(known_turn_ids):
            turn_events = sorted(events_by_session_turn[(session_id, turn_id)], key=lambda event: (event.get("timestamp_ms") is None, event.get("timestamp_ms") or 0, event.get("line", 0)))
            completed = completed_turns.get(turn_id, {})
            start_event = events_by_id.get(completed.get("start_event_id")) if completed else None
            end_event = events_by_id.get(completed.get("end_event_id")) if completed else None
            inferred_start, inferred_end = _event_bounds(turn_events)
            start_event = start_event or inferred_start
            end_event = end_event or (inferred_end if completed else None)
            full_turn_included = bool(
                completed
                and completed.get("start_event_id") in events_by_id
                and completed.get("end_event_id") in events_by_id
            )
            turn_span = _span(
                f"turn:{session_id}:{turn_id}", "turn", parent_span_id=span["span_id"],
                session_id=session_id, turn_id=turn_id,
                event_ids=[event["event_id"] for event in turn_events], start=start_event, end=end_event,
                duration_ms=completed.get("duration_ms") if full_turn_included else None,
                attributes={
                    "outcome": completed.get("outcome") if completed else None,
                    "time_to_first_token_ms": completed.get("time_to_first_token_ms") if completed else None,
                },
            )
            spans.append(turn_span)
            for event_id in turn_span["event_ids"]:
                event_to_span[event_id].append(turn_span["span_id"])

        for call in (event for event in events if event.get("kind") == "tool_call"):
            details = call.get("details", {})
            output_ids = details.get("output_event_ids") if isinstance(details.get("output_event_ids"), list) else []
            outputs = [events_by_id[event_id] for event_id in output_ids if event_id in events_by_id]
            if output_ids and len(outputs) != len(output_ids):
                warnings.append(f"{call['event_id']}: one or more linked tool outputs are missing")
            if not outputs:
                warnings.append(f"{call['event_id']}: tool call has no linked output")
            end = outputs[-1] if outputs else None
            turn_id = call.get("turn_id") if isinstance(call.get("turn_id"), str) and call.get("turn_id") else None
            parent = f"turn:{session_id}:{turn_id}" if turn_id else span["span_id"]
            duration = details.get("reported_wall_time_ms")
            if not isinstance(duration, (int, float)):
                duration = call.get("duration", {}).get("observed_ms")
            tool_span = _span(
                f"tool:{call['event_id']}", "tool", parent_span_id=parent, session_id=session_id,
                turn_id=turn_id, event_ids=[call["event_id"], *[output["event_id"] for output in outputs]],
                start=call, end=end, duration_ms=duration,
                attributes=_tool_attributes(call, outputs),
            )
            spans.append(tool_span)
            for event_id in tool_span["event_ids"]:
                event_to_span[event_id].append(tool_span["span_id"])

        ordered_events = sorted(events, key=lambda event: (event.get("timestamp_ms") is None, event.get("timestamp_ms") or 0, event.get("line", 0)))
        previous: dict[str, Any] | None = None
        for event in ordered_events:
            if event.get("kind") == "reasoning":
                turn_id = event.get("turn_id") if isinstance(event.get("turn_id"), str) and event.get("turn_id") else None
                parent = f"turn:{session_id}:{turn_id}" if turn_id else span["span_id"]
                reasoning_span = _span(
                    f"reasoning:{event['event_id']}", "reasoning", parent_span_id=parent,
                    session_id=session_id, turn_id=turn_id,
                    event_ids=[item["event_id"] for item in (previous, event) if item is not None],
                    start=previous, end=event,
                    attributes={"previous_event_id": previous.get("event_id") if previous else None},
                )
                spans.append(reasoning_span)
                for event_id in reasoning_span["event_ids"]:
                    event_to_span[event_id].append(reasoning_span["span_id"])
            previous = event

    return {
        "analysis_version": SPAN_ANALYSIS_VERSION,
        "source": {
            "root_session_id": root_id,
            "breakdown_schema_version": breakdown.get("schema_version"),
            "breakdown_sha256": breakdown_sha256(breakdown),
        },
        "spans": spans,
        "event_to_span": dict(sorted(event_to_span.items())),
        "included_event_count": len(events_by_id),
        "excluded_event_count": 0,
        "warnings": warnings,
    }
