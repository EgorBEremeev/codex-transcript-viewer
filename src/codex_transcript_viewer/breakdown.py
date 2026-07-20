"""Build a compact, lossless analytical breakdown of a local Codex session tree."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .discovery import build_tree, read_session_meta
from .parser import _native_boundary


BREAKDOWN_SCHEMA_VERSION = 1
_WALL_TIME = re.compile(r"\bWall time\s+([0-9]+(?:\.[0-9]+)?)\s*seconds?\b", re.I)
_NESTED_TOOL = re.compile(r"\btools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _timestamp_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return round(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _json_size(value: Any) -> tuple[int, int]:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return len(encoded), len(encoded.encode("utf-8"))


def _text_size(value: Any) -> tuple[int, int]:
    text = value if isinstance(value, str) else ""
    return len(text), len(text.encode("utf-8"))


def _content_size(value: Any) -> dict[str, Any]:
    """Measure content without placing its potentially large body in the result."""
    serialized_chars, serialized_bytes = _json_size(value)
    if isinstance(value, str):
        chars, utf8_bytes = _text_size(value)
        return {
            "content_type": "string",
            "text_chars": chars,
            "utf8_bytes": utf8_bytes,
            "serialized_json_chars": serialized_chars,
            "serialized_json_utf8_bytes": serialized_bytes,
            "blocks": [],
        }
    if isinstance(value, list):
        blocks = []
        text_chars = 0
        utf8_bytes = 0
        for index, block in enumerate(value):
            if not isinstance(block, dict):
                continue
            item: dict[str, Any] = {"index": index, "type": str(block.get("type") or "")}
            for key in ("text", "encrypted_content"):
                if isinstance(block.get(key), str):
                    chars, size = _text_size(block[key])
                    item[f"{key}_chars"] = chars
                    item[f"{key}_utf8_bytes"] = size
                    if key == "text":
                        text_chars += chars
                        utf8_bytes += size
            blocks.append(item)
        return {
            "content_type": "blocks",
            "text_chars": text_chars,
            "utf8_bytes": utf8_bytes,
            "serialized_json_chars": serialized_chars,
            "serialized_json_utf8_bytes": serialized_bytes,
            "blocks": blocks,
        }
    return {
        "content_type": "json" if value is not None else "null",
        "text_chars": 0,
        "utf8_bytes": 0,
        "serialized_json_chars": serialized_chars,
        "serialized_json_utf8_bytes": serialized_bytes,
        "blocks": [],
    }


def _summarize_large_text(value: Any) -> Any:
    if isinstance(value, str):
        return {
            "chars": len(value),
            "utf8_bytes": len(value.encode("utf-8")),
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        }
    if isinstance(value, dict):
        return {key: _summarize_large_text(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_summarize_large_text(item) for item in value]
    return value


def _safe_meta(payload: dict[str, Any]) -> dict[str, Any]:
    meta = deepcopy(payload)
    instructions = meta.get("base_instructions")
    if isinstance(instructions, dict) and isinstance(instructions.get("text"), str):
        preserved = {key: value for key, value in instructions.items() if key != "text"}
        preserved["text"] = _summarize_large_text(instructions["text"])
        meta["base_instructions"] = preserved
    elif isinstance(instructions, str):
        meta["base_instructions"] = _summarize_large_text(instructions)
    return meta


def _payload_type(entry: dict[str, Any], payload: dict[str, Any]) -> str:
    return str(payload.get("type") or entry.get("type") or "unknown")


def _kind(outer_type: str, payload_type: str) -> str:
    if outer_type == "response_item":
        return {
            "custom_tool_call": "tool_call",
            "function_call": "tool_call",
            "image_generation_call": "tool_call",
            "tool_search_call": "tool_call",
            "web_search_call": "tool_call",
            "custom_tool_call_output": "tool_output",
            "function_call_output": "tool_output",
            "tool_search_call_output": "tool_output",
        }.get(payload_type, payload_type)
    return payload_type


def _inventory(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return {"payload_shape": "object", "payload_keys": sorted(payload)}
    if isinstance(payload, list):
        return {"payload_shape": "array", "payload_length": len(payload)}
    return {"payload_shape": type(payload).__name__}


def _extract_command_name(command: str) -> str | None:
    for segment in re.split(r"[;|\r\n]+", command):
        segment = segment.strip()
        segment = re.sub(r"^(?:\$?[A-Za-z_][\w:]*\s*=\s*[^;]+\s*)+", "", segment)
        match = re.search(r"[A-Za-z][A-Za-z0-9_-]*", segment)
        if match:
            return match.group(0)
    return None


def _decode_command(argument: str) -> str | None:
    for pattern in (
        r'"command"\s*:\s*"((?:\\.|[^"\\])*)"',
        r"'command'\s*:\s*'((?:\\.|[^'\\])*)'",
        r'\bcommand\s*:\s*"((?:\\.|[^"\\])*)"',
        r"\bcommand\s*:\s*'((?:\\.|[^'\\])*)'",
    ):
        match = re.search(pattern, argument, re.S)
        if not match:
            continue
        raw = match.group(1)
        try:
            return json.loads(f'"{raw}"')
        except json.JSONDecodeError:
            return raw.replace("\\'", "'").replace("\\\\", "\\")
    return None


def _balanced_call_source(source: str, start: int) -> str | None:
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[start + 1:index]
    return None


def _nested_calls(source: Any) -> list[dict[str, Any]]:
    if not isinstance(source, str):
        return []
    result = []
    for match in _NESTED_TOOL.finditer(source):
        argument = _balanced_call_source(source, match.end() - 1)
        if argument is None:
            continue
        tool = match.group(1)
        command = _decode_command(argument) if tool == "shell_command" else None
        item: dict[str, Any] = {
            "tool": tool,
            "extraction": {"method": "balanced_call_scan", "confidence": "high"},
        }
        if command is not None:
            item.update({
                "command_name": _extract_command_name(command),
                "command_preview": command[:128],
                "command_chars": len(command),
                "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            })
        result.append(item)
    return result


def _token_delta(previous: Any, current: Any) -> Any:
    if isinstance(previous, dict) and isinstance(current, dict):
        return {key: _token_delta(previous.get(key), value) for key, value in current.items()}
    if isinstance(previous, (int, float)) and isinstance(current, (int, float)):
        return current - previous
    return None


def _has_negative_number(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_negative_number(item) for item in value.values())
    return isinstance(value, (int, float)) and value < 0


def _add_context_metric(target: dict[str, int], size: dict[str, Any], *, encrypted: int = 0) -> None:
    target["text_chars"] += int(size.get("text_chars") or 0)
    target["utf8_bytes"] += int(size.get("utf8_bytes") or 0)
    target["serialized_json_chars"] += int(size.get("serialized_json_chars") or 0)
    target["encrypted_chars"] += encrypted


def _context_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        origin: {"text_chars": 0, "utf8_bytes": 0, "serialized_json_chars": 0, "encrypted_chars": 0}
        for origin in ("native", "inherited", "unknown")
    }
    by_kind: Counter[str] = Counter()
    for event in events:
        origin = event["record_origin"]
        details = event["details"]
        for label in ("input_size", "arguments_size", "output_size", "content_size", "message_size", "summary_size"):
            size = details.get(label)
            if isinstance(size, dict):
                encrypted = sum(int(block.get("encrypted_content_chars") or 0) for block in size.get("blocks", []))
                _add_context_metric(totals[origin], size, encrypted=encrypted)
                by_kind[label] += int(size.get("text_chars") or 0)
        totals[origin]["encrypted_chars"] += int(details.get("encrypted_content_chars") or 0)
    return {**totals, "by_content_kind": dict(sorted(by_kind.items()))}


def _event_details(outer_type: str, payload_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = _inventory(payload)
    payload_chars, payload_bytes = _json_size(payload)
    details["payload_size"] = {"serialized_json_chars": payload_chars, "serialized_json_utf8_bytes": payload_bytes}
    if outer_type == "response_item" and payload_type in {"function_call", "custom_tool_call"}:
        details.update({key: payload.get(key) for key in ("id", "call_id", "name", "namespace", "status") if key in payload})
        if "input" in payload:
            details["input_size"] = _content_size(payload["input"])
        if "arguments" in payload:
            details["arguments_size"] = _content_size(payload["arguments"])
        if payload_type == "custom_tool_call" and payload.get("name") == "exec":
            details["nested_calls"] = _nested_calls(payload.get("input"))
    elif outer_type == "response_item" and payload_type in {"function_call_output", "custom_tool_call_output", "tool_search_call_output"}:
        details.update({key: payload.get(key) for key in ("id", "call_id") if key in payload})
        details["output_size"] = _content_size(payload.get("output"))
        text = payload.get("output") if isinstance(payload.get("output"), str) else ""
        match = _WALL_TIME.search(text)
        if match:
            details["reported_wall_time_ms"] = round(float(match.group(1)) * 1000)
    elif outer_type == "response_item" and payload_type in {"message", "agent_message"}:
        details.update({key: payload.get(key) for key in ("id", "role", "phase", "author", "recipient") if key in payload})
        details["content_size"] = _content_size(payload.get("content", []))
    elif outer_type == "response_item" and payload_type == "reasoning":
        details["id"] = payload.get("id")
        details["summary_size"] = _content_size(payload.get("summary", []))
        encrypted = payload.get("encrypted_content")
        if isinstance(encrypted, str):
            details["encrypted_content_chars"] = len(encrypted)
            details["encrypted_content_utf8_bytes"] = len(encrypted.encode("utf-8"))
    elif payload_type in {"user_message", "agent_message", "task_complete"}:
        text = payload.get("message", payload.get("last_agent_message", payload.get("text")))
        details["message_size"] = _content_size(text)
        for key in ("phase", "memory_citation"):
            if key in payload:
                details[key] = payload[key]
    elif payload_type == "token_count":
        details["info"] = deepcopy(payload.get("info") or {})
        details["rate_limits"] = deepcopy(payload.get("rate_limits"))
    elif payload_type in {"task_started", "task_complete", "turn_aborted"}:
        for key in ("turn_id", "started_at", "completed_at", "duration_ms", "time_to_first_token_ms", "reason"):
            if key in payload:
                details[key] = payload[key]
    elif payload_type == "sub_agent_activity":
        for key in ("event_id", "occurred_at_ms", "agent_thread_id", "agent_path", "kind"):
            if key in payload:
                details[key] = payload[key]
    return details


def _parse_session(path: Path, node: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    records: list[tuple[int, dict[str, Any] | None, str | None]] = []
    warnings: list[str] = []
    with path.open(encoding="utf-8") as source:
        for line, raw in enumerate(source, 1):
            try:
                entry = json.loads(raw)
                records.append((line, entry if isinstance(entry, dict) else None, None))
            except json.JSONDecodeError as error:
                records.append((line, None, error.msg))
                warnings.append(f"{path.name}: line {line}: invalid JSON: {error.msg}")

    meta = read_session_meta(path)
    boundary = _native_boundary(records, (records[0][0], {"payload": meta}, None)) if node.get("parent_id") and records else None
    if node.get("parent_id") and boundary is None:
        warnings.append(f"{node['id']}: subagent native boundary not found")
    events: list[dict[str, Any]] = []
    current_turn = ""
    meta_snapshots = []
    for seq, (line, entry, parse_error) in enumerate(records, 1):
        if entry is None:
            outer_type = "parse_error"
            payload: dict[str, Any] = {}
            payload_type = "parse_error"
            origin = "unknown" if boundary is None and node.get("parent_id") else ("inherited" if boundary and line < boundary else "native")
            details = {"error": parse_error, "raw_chars": len(""), "payload_shape": "invalid_json"}
            timestamp = ""
        else:
            outer_type = str(entry.get("type") or "unknown")
            raw_payload = entry.get("payload")
            payload = raw_payload if isinstance(raw_payload, dict) else {}
            payload_type = _payload_type(entry, payload)
            origin = "unknown" if boundary is None and node.get("parent_id") else ("inherited" if boundary and line < boundary else "native")
            if outer_type == "session_meta" and line == records[0][0]:
                origin = "native"
            timestamp = str(entry.get("timestamp") or "")
            details = _event_details(outer_type, payload_type, payload)
        explicit_turn = payload.get("turn_id")
        passthrough = payload.get("internal_chat_message_metadata_passthrough")
        if not explicit_turn and isinstance(passthrough, dict):
            explicit_turn = passthrough.get("turn_id")
        turn_id = str(explicit_turn or current_turn or "")
        if payload_type == "task_started" and payload.get("turn_id"):
            current_turn = str(payload["turn_id"])
            turn_id = current_turn
        event = {
            "event_id": f"{node['id']}:{line}",
            "line": line,
            "seq": seq,
            "timestamp": timestamp,
            "timestamp_ms": _timestamp_ms(timestamp),
            "outer_type": outer_type,
            "payload_type": payload_type,
            "kind": _kind(outer_type, payload_type),
            "turn_id": turn_id,
            "record_origin": origin,
            "duration": {"observed_ms": None, "reported_ms": None, "source": None},
            "details": details,
        }
        if payload_type in {"task_complete", "turn_aborted"} and isinstance(payload.get("duration_ms"), (int, float)):
            event["duration"] = {"observed_ms": None, "reported_ms": payload["duration_ms"], "source": "event_payload"}
        if outer_type == "session_meta":
            snapshot = _safe_meta(payload)
            meta_snapshots.append({"line": line, "timestamp": timestamp, "fingerprint": hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()})
        events.append(event)

    _derive_tool_links(events, warnings)
    _derive_tokens(events)
    turns = _turn_spans(events)
    events.sort(key=lambda event: (event["timestamp_ms"] is None, event["timestamp_ms"] or 0, event["line"]))
    scope = Counter(event["record_origin"] for event in events)
    session = {
        "session_id": node["id"],
        "parent_session_id": node.get("parent_id") or "",
        "agent_path": node.get("agent_path") or "",
        "source_path": str(path),
        "meta": _safe_meta(meta),
        "meta_snapshots": meta_snapshots,
        "record_scope": {"source_records": len(events), "native_records": scope["native"], "inherited_records": scope["inherited"], "unknown_records": scope["unknown"], "native_boundary_line": boundary},
        "metrics": _session_metrics(events),
        "turns": turns,
        "events": events,
    }
    return session, warnings


def _derive_tool_links(events: list[dict[str, Any]], warnings: list[str]) -> None:
    calls: dict[str, dict[str, Any]] = {}
    for event in events:
        details = event["details"]
        call_id = details.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        if event["kind"] == "tool_call":
            calls[call_id] = event
            details["output_event_ids"] = []
        elif event["kind"] == "tool_output":
            call = calls.get(call_id)
            if call is None:
                warnings.append(f"{event['event_id']}: tool output has no preceding call")
                continue
            details["paired_call_event_id"] = call["event_id"]
            call["details"]["output_event_ids"].append(event["event_id"])
            start, end = call["timestamp_ms"], event["timestamp_ms"]
            if start is not None and end is not None and end >= start:
                call["duration"]["observed_ms"] = end - start
                call["duration"]["source"] = "call_to_output_timestamp"
            else:
                warnings.append(f"{event['event_id']}: tool pair has invalid timestamps")
            reported = details.get("reported_wall_time_ms")
            if isinstance(reported, int):
                call["duration"]["reported_ms"] = reported


def _derive_tokens(events: list[dict[str, Any]]) -> None:
    previous: Any = None
    previous_id: str | None = None
    for event in events:
        if event["payload_type"] != "token_count":
            continue
        total = event["details"].get("info", {}).get("total_token_usage")
        if not isinstance(total, dict):
            continue
        delta = _token_delta(previous, total) if previous is not None else _token_delta({}, total)
        event["details"]["delta_from_previous_total"] = delta
        event["details"]["reset_detected"] = _has_negative_number(delta)
        if previous == total and previous_id:
            event["details"]["is_duplicate_snapshot"] = True
            event["details"]["duplicate_of_event_id"] = previous_id
        else:
            event["details"]["is_duplicate_snapshot"] = False
        previous, previous_id = deepcopy(total), event["event_id"]


def _turn_spans(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    starts: dict[str, dict[str, Any]] = {}
    spans = []
    for event in events:
        turn_id = event["turn_id"]
        if not turn_id:
            continue
        if event["payload_type"] == "task_started":
            starts[turn_id] = event
        elif event["payload_type"] in {"task_complete", "turn_aborted"} and turn_id in starts:
            start = starts.pop(turn_id)
            spans.append({
                "turn_id": turn_id,
                "start_at": start["timestamp"],
                "end_at": event["timestamp"],
                "duration_ms": event["details"].get("duration_ms"),
                "time_to_first_token_ms": event["details"].get("time_to_first_token_ms"),
                "outcome": "completed" if event["payload_type"] == "task_complete" else "aborted",
                "start_event_id": start["event_id"],
                "end_event_id": event["event_id"],
            })
    return spans


def _session_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    native = [event for event in events if event["record_origin"] == "native"]
    token_events = [event for event in native if event["payload_type"] == "token_count"]
    cumulative = {}
    if token_events:
        cumulative = deepcopy(token_events[-1]["details"].get("info", {}).get("total_token_usage") or {})
    return {
        "events": {"native_by_kind": dict(Counter(event["kind"] for event in native)), "all_by_kind": dict(Counter(event["kind"] for event in events))},
        "context_material": _context_summary(events),
        "reported_token_usage": {"snapshot_count": len(token_events), "last_cumulative": cumulative, "resets": [event["event_id"] for event in token_events if event["details"].get("reset_detected")]},
        "tools": {"native_calls": sum(event["kind"] == "tool_call" for event in native), "native_outputs": sum(event["kind"] == "tool_output" for event in native)},
    }


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


def _tree_metrics(sessions: list[dict[str, Any]], timeline: list[dict[str, Any]]) -> dict[str, Any]:
    dated = [item for item in timeline if item["timestamp_ms"] is not None]
    timed = [item["timestamp_ms"] for item in dated]
    event_counts: Counter[str] = Counter()
    tool_calls = 0
    tool_outputs = 0
    token_usage: dict[str, Any] = {}
    context: dict[str, Any] = {}
    for session in sessions:
        metrics = session["metrics"]
        event_counts.update(metrics["events"]["native_by_kind"])
        tool_calls += metrics["tools"]["native_calls"]
        tool_outputs += metrics["tools"]["native_outputs"]
        _add_numeric(token_usage, metrics["reported_token_usage"]["last_cumulative"])
        _add_numeric(context, metrics["context_material"])
    return {
        "wall_clock": {
            "started_at": dated[0]["timestamp"] if dated else "",
            "ended_at": dated[-1]["timestamp"] if dated else "",
            "duration_ms": max(timed) - min(timed) if timed else None,
        },
        "session_count": len(sessions),
        "events": {"native_by_kind": dict(event_counts), "native_total": sum(event_counts.values())},
        "tools": {"native_calls": tool_calls, "native_outputs": tool_outputs},
        "reported_token_usage": {"sum_session_cumulative": token_usage},
        "context_material": context,
    }


def build_breakdown(reference: str, sessions_dir: str | Path | None = None) -> dict[str, Any]:
    """Return a lossless analytics dataset for a local root session and descendants."""
    tree = build_tree(reference, sessions_dir)
    sessions = []
    warnings: list[str] = []
    for node in tree["nodes"]:
        session, session_warnings = _parse_session(Path(node["path"]), node)
        sessions.append(session)
        warnings.extend(session_warnings)
    timeline = [
        {"event_id": event["event_id"], "session_id": session["session_id"], "timestamp": event["timestamp"], "timestamp_ms": event["timestamp_ms"], "line": event["line"]}
        for session in sessions for event in session["events"]
    ]
    timeline.sort(key=lambda item: (item["timestamp_ms"] is None, item["timestamp_ms"] or 0, item["line"], item["session_id"]))
    return {
        "schema_version": BREAKDOWN_SCHEMA_VERSION,
        "root_session_id": tree["root_id"],
        "tree_metrics": _tree_metrics(sessions, timeline),
        "sessions": sessions,
        "timeline": timeline,
        "warnings": warnings,
    }
