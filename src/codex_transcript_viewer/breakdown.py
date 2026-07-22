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


BREAKDOWN_SCHEMA_VERSION = 2
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


def _command_label(name: str, arguments: str) -> str:
    return f"{name} {arguments}".strip()


def _command_segments(command: str) -> list[str]:
    """Split a PowerShell command without cutting quoted arguments."""
    segments: list[str] = []
    start = 0
    quote = ""
    for index, char in enumerate(command):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char in {";", "|", "\r", "\n"}:
            segment = command[start:index].strip()
            if segment:
                segments.append(segment)
            start = index + 1
    tail = command[start:].strip()
    if tail:
        segments.append(tail)
    return segments


def _parse_wam_mplan(segment: str) -> dict[str, Any] | None:
    match = re.search(r"\bwam_mplan(?:\.exe)?\s+(get-many|get|find|tail|validate-store)\b\s*(.*)", segment, re.I | re.S)
    if not match:
        return None
    operation, remainder = match.groups()
    tokens = re.findall(r"(?:[^\s\"']+|\"[^\"]*\"|'[^']*')+", remainder)
    result: dict[str, Any] = {
        "command_name": "wam_mplan",
        "command_operation": operation,
        "command_arguments": remainder.strip(),
        "command_label": _command_label("wam_mplan", f"{operation} {remainder.strip()}"),
    }
    if tokens:
        result["store_path"] = tokens[0].strip("\"'")
    positionals: list[str] = []
    options: dict[str, list[str]] = {}
    index = 1
    while index < len(tokens):
        token = tokens[index].strip("\"'")
        if token.startswith("--"):
            key, separator, value = token[2:].partition("=")
            if not separator:
                value = tokens[index + 1].strip("\"'") if index + 1 < len(tokens) else ""
                index += 1
            options.setdefault(key, []).append(value)
        else:
            positionals.append(token)
        index += 1
    if operation in {"get", "get-many"}:
        result["identities"] = positionals
    if operation == "find":
        result["where"] = options.get("where", [])
        result["limit"] = next(iter(options.get("limit", [])), None)
        result["collection"] = next(iter(options.get("collection", [])), None)
    if operation == "tail":
        result["count"] = next(iter(options.get("count", [])), None)
    result["format"] = next(iter(options.get("format", [])), None)
    return result


def _command_invocations(command: str) -> list[dict[str, Any]]:
    """Project common nested shell invocations into a stable, useful shape."""
    heredoc_python = re.search(r"@(?P<quote>['\"])[\s\S]*?(?P=quote)@\s*\|\s*&?\s*(?:'(?P<single>[^']*python(?:\.exe)?)'|\"(?P<double>[^\"]*python(?:\.exe)?)\"|(?P<bare>[^\s]+python(?:\.exe)?))\s+-", command, re.I)
    if heredoc_python:
        return [{
            "command_name": "python",
            "command_label": "python (stdin script)",
            "command_path": next(value for value in heredoc_python.group("single", "double", "bare") if value is not None),
            "command_kind": "python_stdin_script",
        }]
    result: list[dict[str, Any]] = []
    for segment in _command_segments(command):
        wam = _parse_wam_mplan(segment)
        if wam is not None:
            result.append(wam)
            continue
        git = re.search(r"(?:^|\s)git(?:\.exe)?\s+(.*)$", segment, re.I | re.S)
        if git:
            tokens = re.findall(r"(?:[^\s\"']+|\"[^\"]*\"|'[^']*')+", git.group(1))
            index = 0
            global_options: list[str] = []
            options_with_value = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
            while index < len(tokens) and tokens[index].startswith("-"):
                option = tokens[index]
                global_options.append(option)
                if option in options_with_value and index + 1 < len(tokens):
                    index += 1
                    global_options.append(tokens[index])
                index += 1
            operation = tokens[index] if index < len(tokens) else ""
            arguments = " ".join(tokens[index + 1:]) if operation else " ".join(tokens)
            result.append({"command_name": "git", "command_operation": operation or None, "command_arguments": arguments, "command_label": _command_label("git", f"{operation} {arguments}"), "git_global_options": global_options})
            continue
        python = re.search(r"(?:^|\s)&?\s*['\"]?([A-Za-z]:[^'\"\s]*\\python(?:\.exe)?|[^'\"\s]*python(?:\.exe)?)['\"]?(?:\s|$)(.*)", segment, re.I | re.S)
        if python:
            executable, arguments = python.groups()
            result.append({"command_name": "python", "command_path": executable, "command_arguments": arguments.strip(), "command_label": _command_label("python", arguments.strip())})
            continue
        cleaned = re.sub(r"^(?:\$?[A-Za-z_][\w:]*\s*=\s*[^;]+\s*)+", "", segment).lstrip("& ")
        match = re.search(r"[A-Za-z][A-Za-z0-9_-]*", cleaned)
        if match:
            name = match.group(0)
            arguments = cleaned[match.end():].strip()
            result.append({"command_name": name, "command_arguments": arguments, "command_label": _command_label(name, arguments)})
    return result


def _decode_command(argument: str) -> str | None:
    for pattern in (
        r'"command"\s*:\s*"((?:\\.|[^"\\])*)"',
        r"'command'\s*:\s*'((?:\\.|[^'\\])*)'",
        r'"command"\s*:\s*`((?:\\.|[^`\\])*)`',
        r"'command'\s*:\s*`((?:\\.|[^`\\])*)`",
        r'\bcommand\s*:\s*"((?:\\.|[^"\\])*)"',
        r"\bcommand\s*:\s*'((?:\\.|[^'\\])*)'",
        r'\bcommand\s*:\s*`((?:\\.|[^`\\])*)`',
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
        projections = _command_invocations(command) if command is not None else [{}]
        for projection in projections:
            item: dict[str, Any] = {
                "tool": tool,
                "extraction": {"method": "balanced_call_scan", "confidence": "high"},
                **projection,
            }
            if command is not None:
                item.update({
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


def build_breakdown(reference: str, sessions_dir: str | Path | None = None) -> dict[str, Any]:
    """Return a lossless raw event tree for a local root session and descendants."""
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
        "sessions": sessions,
        "timeline": timeline,
        "warnings": warnings,
    }
