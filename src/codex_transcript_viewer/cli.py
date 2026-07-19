"""Command-line interface for Codex session transcripts."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import webbrowser
from collections import deque
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Iterable

from .discovery import (
    build_tree,
    default_sessions_dir,
    list_sessions,
    read_session_meta,
    session_files,
    session_summary,
)
from .breakdown import build_breakdown
from .html_builder import build_html
from .parser import SCHEMA_VERSION, iter_normalized, load_conversation
from .transport import build_remote_tree, open_session_source, parse_remote_reference


_SECRET_KEY = re.compile(
    r"password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"bearer[_-]?token|authorization|cookie|(?:^|[_-])token$",
    re.I,
)
_SECRET_LABEL = (
    r"(?:[A-Za-z0-9]+[_-])*(?:password|secret[_-]?access[_-]?key|secret|"
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|bearer[_-]?token|token)"
)
_ASSIGNED_SECRET = re.compile(
    rf"(\b{_SECRET_LABEL}\b\s*(?:=|:)\s*)([\"']?)([^\s,\"';]+)",
    re.I,
)
_HEADER_SECRET = re.compile(
    r"(\b(?:authorization|cookie)\b\s*:\s*)([\"']?)([^\s,\"';]+(?:\s+[^\s,\"';]+)?)",
    re.I,
)
_BEARER_SECRET = re.compile(r"(\bbearer\s+)([^\s,\"';]+)", re.I)


def _version() -> str:
    try:
        return version("codex-transcript-viewer")
    except PackageNotFoundError:
        return "0.5.0"


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value).strip(".") or "session"


def _open_private(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)
    else:
        os.chmod(path, 0o600)
    os.ftruncate(descriptor, 0)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def _resolve_output(value: str | Path, source: Path) -> Path:
    output = Path(value).expanduser().resolve()
    resolved_source = source.expanduser().resolve()
    if output == resolved_source or (
        output.exists() and resolved_source.exists() and output.samefile(resolved_source)
    ):
        raise ValueError(f"output path is the source transcript: {output}")
    return output


def _write_private(path: Path, text: str) -> None:
    with _open_private(path) as output:
        output.write(text)


def _open_browser(uri: str) -> bool:
    if not sys.platform.startswith("linux"):
        return webbrowser.open(uri)
    try:
        subprocess.Popen(
            ["xdg-open", uri],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if _SECRET_KEY.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        value = _HEADER_SECRET.sub(r"\1\2<redacted>", value)
        value = _BEARER_SECRET.sub(r"\1<redacted>", value)
        return _ASSIGNED_SECRET.sub(r"\1\2<redacted>", value)
    return value


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _truncate(value: Any, limit: int = 2000) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return {"text": value[:limit], "truncated": len(value) - limit, "length": len(value)}
    if isinstance(value, (dict, list)):
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded) > limit:
            return {"preview": encoded[:limit], "truncated": len(encoded) - limit, "length": len(encoded)}
    return value


def _prepare_event(event: dict[str, Any], *, compact: bool, redact: bool) -> dict[str, Any]:
    if compact:
        keep_raw = event.get("kind") in {"unknown", "parse_error"}
        prepared = {
            key: _truncate(value)
            for key, value in event.items()
            if key != "raw" or keep_raw
        }
    else:
        prepared = dict(event)
    prepared["schema_version"] = SCHEMA_VERSION
    return _redact(prepared) if redact else prepared


def _matches(event: dict[str, Any], args: argparse.Namespace) -> bool:
    kind = getattr(args, "kind", None)
    if kind:
        actual = str(event.get("kind", ""))
        if kind == "message":
            if actual != "message" and not actual.endswith("_message"):
                return False
        elif actual != kind:
            return False
    record_type = getattr(args, "type", None)
    if record_type and record_type not in {event.get("outer_type"), event.get("raw_type")}:
        return False
    checks = {
        "turn": "turn_id",
        "role": "role",
        "phase": "phase",
        "tool": "name",
        "call_id": "call_id",
    }
    for argument, field in checks.items():
        wanted = getattr(args, argument, None)
        if wanted and str(event.get(field, "")) != wanted:
            return False
    text = getattr(args, "text", None)
    if text and text.casefold() not in json.dumps(event, ensure_ascii=False).casefold():
        return False
    return True


def _conversation_event(event: dict[str, Any]) -> dict[str, Any] | None:
    outer_type = event.get("outer_type")
    kind = event.get("kind")
    if outer_type == "response_item" and kind == "message":
        if event.get("role") not in {"user", "assistant"}:
            return None
        canonical = dict(event)
        if canonical["role"] == "user":
            canonical["phase"] = ""
        elif canonical.get("phase") != "final_answer":
            canonical["phase"] = canonical.get("phase") or "commentary"
        return canonical
    if outer_type != "event_msg":
        return None

    role = None
    phase = ""
    if kind == "user_message":
        role = "user"
    elif kind == "agent_message":
        role = "assistant"
        phase = str(event.get("phase") or "commentary")
    elif kind == "task_complete" and event.get("text"):
        role = "assistant"
        phase = "final_answer"
    if role is None:
        return None

    canonical = dict(event)
    canonical.update({"kind": "message", "role": role, "phase": phase})
    return canonical


def _conversation_events(events: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    seen: dict[tuple[str, str, str, str], tuple[str, str]] = {}
    for event in events:
        candidate = _conversation_event(event)
        if candidate is None:
            continue
        fingerprint = (
            str(candidate.get("turn_id", "")),
            str(candidate.get("role", "")),
            str(candidate.get("phase", "")),
            str(candidate.get("text", "")),
        )
        representation = (
            str(candidate.get("outer_type", "")),
            str(candidate.get("raw_type", "")),
        )
        previous_representation = seen.get(fingerprint)
        if previous_representation is not None and previous_representation != representation:
            continue
        seen.setdefault(fingerprint, representation)
        yield candidate


def _selected_events(path: Path, args: argparse.Namespace) -> Iterable[dict[str, Any]]:
    events: Iterable[dict[str, Any]] = iter_normalized(
        path,
        include_inherited=getattr(args, "include_inherited", False),
        include_raw=not getattr(args, "compact", False),
    )
    if getattr(args, "view", "events") == "conversation":
        events = _conversation_events(events)

    selected: Iterable[dict[str, Any]] = (
        _prepare_event(event, compact=args.compact, redact=args.redact)
        for event in events
        if _matches(event, args)
    )
    last = getattr(args, "last", None)
    if last is not None:
        yield from deque(selected, maxlen=last)
        return

    count = 0
    for event in selected:
        yield event
        count += 1
        if args.limit is not None and count >= args.limit:
            return


def _event_markdown(event: dict[str, Any]) -> str:
    label = event.get("kind", "event")
    detail = event.get("role") or event.get("name") or event.get("raw_type", "")
    heading = f"### {event.get('seq', '?')} · {label}" + (f" · {detail}" if detail else "")
    body = event.get("text")
    if body is None:
        body = event.get("arguments", event.get("input", event.get("output")))
    if body is None:
        body = {key: value for key, value in event.items() if key not in {"raw", "schema_version"}}
    if not isinstance(body, str):
        body = "```json\n" + json.dumps(body, ensure_ascii=False, indent=2) + "\n```"
    return f"{heading}\n\n{body}\n"


def _emit_events(events: Iterable[dict[str, Any]], fmt: str, destination: Path | None) -> int:
    if fmt == "json":
        materialized = list(events)
        text = json.dumps(
            {"schema_version": SCHEMA_VERSION, "events": materialized},
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        count = len(materialized)
        if destination:
            _write_private(destination, text)
        else:
            sys.stdout.write(text)
        return count

    stream = sys.stdout
    close_stream = False
    if destination:
        destination.parent.mkdir(parents=True, exist_ok=True)
        stream = _open_private(destination)
        close_stream = True
    count = 0
    try:
        for event in events:
            if fmt == "jsonl":
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            else:
                stream.write(_event_markdown(event) + "\n")
            count += 1
    finally:
        if close_stream:
            stream.close()
    return count


def _render(path: Path, output: Path, args: argparse.Namespace) -> dict[str, Any]:
    meta, events = load_conversation(
        path,
        include_inherited=args.include_inherited,
    )
    if args.redact:
        meta, events = _redact(meta), _redact(events)
    _write_private(output, build_html(meta, events))
    return {
        "path": str(output),
        "bytes": output.stat().st_size,
        "events": len(events),
        "session_id": meta.get("id", ""),
        "warnings": [] if meta else ["session_meta not found"],
    }


def _breakdown(reference: str, sessions_dir: str | Path | None, args: argparse.Namespace) -> dict[str, Any]:
    local_candidate = Path(reference).expanduser()
    if not local_candidate.is_file() and parse_remote_reference(reference) is not None:
        raise ValueError("breakdown currently supports local sessions only")
    data = build_breakdown(reference, sessions_dir)
    if args.redact:
        data = _redact(data)
    if args.output != "-":
        output = _resolve_output(args.output, Path(data["sessions"][0]["source_path"]))
        sources = [Path(session["source_path"]).resolve() for session in data["sessions"]]
        if output in sources:
            raise ValueError(f"output path is a source transcript: {output}")
        _write_private(output, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return {"path": str(output), "sessions": len(data["sessions"]), "root_session_id": data["root_session_id"]}
    return data


def _print_result(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=False))
    elif isinstance(data, str):
        print(data)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def _add_session_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "session",
        help="JSONL path, session ID/prefix, or SSH_HOST:SESSION_ID",
    )
    parser.add_argument("--include-inherited", action="store_true", help="include copied parent history in subagent logs")


def _add_export_flags(parser: argparse.ArgumentParser) -> None:
    _add_session_flags(parser)
    parser.add_argument("--compact", action="store_true", help="omit raw known records and truncate large values")
    parser.add_argument("--redact", action="store_true", help="redact values under secret-like keys")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-transcript", description="Inspect and render Codex CLI session logs")
    parser.add_argument("--json", action="store_true", help="emit a stable JSON command result")
    parser.add_argument("--sessions-dir", help="override the Codex sessions directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="verify local session discovery and runtime")

    listing = subparsers.add_parser("list", help="list recent Codex sessions")
    listing.add_argument("--limit", type=_positive_int, default=20)
    listing.add_argument("--cwd")
    listing.add_argument("--thread-source", choices=("user", "subagent"))

    render = subparsers.add_parser("render", help="render a private self-contained HTML transcript")
    _add_session_flags(render)
    render.add_argument("--output", required=True)
    render.add_argument("--redact", action="store_true")

    browser = subparsers.add_parser("browser", help="render and open a temporary HTML transcript")
    _add_session_flags(browser)
    browser.add_argument("--output", help="reuse this viewer path instead of the private temp directory")
    browser.add_argument("--redact", action="store_true")

    export = subparsers.add_parser("export", help="export normalized transcript events")
    _add_export_flags(export)
    export.add_argument("--limit", type=_positive_int, help="maximum matching events")
    export.add_argument("--format", choices=("jsonl", "json", "markdown"), default="jsonl")
    export.add_argument("--output", default="-", help="output file or - for stdout")

    query = subparsers.add_parser("query", help="filter normalized transcript events")
    _add_export_flags(query)
    query.add_argument("--view", choices=("events", "conversation"), default="events")
    bounds = query.add_mutually_exclusive_group()
    bounds.add_argument("--limit", type=_positive_int, help="maximum matching events")
    bounds.add_argument("--last", type=_positive_int, help="keep the last N matching events")
    query.add_argument("--kind")
    query.add_argument("--type")
    query.add_argument("--turn")
    query.add_argument("--role")
    query.add_argument("--phase")
    query.add_argument("--tool")
    query.add_argument("--call-id")
    query.add_argument("--text")
    query.add_argument("--format", choices=("jsonl", "markdown"), default="jsonl")
    query.add_argument("--output", default="-")

    tree = subparsers.add_parser("tree", help="show parent and subagent session relationships")
    tree.add_argument("session", help="session ID/prefix or SSH_HOST:SESSION_ID")
    tree.add_argument("--format", choices=("text", "json"), default="text")

    raw = subparsers.add_parser("raw", help="read one exact raw JSONL record")
    raw.add_argument("session")
    raw.add_argument("--line", type=int, required=True)
    raw.add_argument("--redact", action="store_true")

    breakdown = subparsers.add_parser("breakdown", help="build a local analytical JSON dataset for a session tree")
    breakdown.add_argument("session", help="JSONL path, session ID/prefix, or unique JSONL basename")
    breakdown.add_argument("--output", default="-", help="output JSON file or - for stdout")
    breakdown.add_argument("--redact", action="store_true")
    return parser


def _tree_text(tree: dict[str, Any]) -> str:
    lines = []
    for node in tree["nodes"]:
        marker = "*" if node["selected"] else "-"
        label = node["agent_path"] or node["thread_source"] or "session"
        lines.append(f"{'  ' * node['depth']}{marker} {node['id']}  {label}  {node['path']}")
    return "\n".join(lines)


def _read_raw(path: Path, line_number: int) -> Any:
    with path.open(encoding="utf-8") as transcript:
        for current, line in enumerate(transcript, 1):
            if current == line_number:
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    return {"line": current, "raw": line.rstrip("\r\n"), "parse_error": True}
    raise ValueError(f"line {line_number} is past end of file")


def run(args: argparse.Namespace) -> None:
    sessions_dir = args.sessions_dir or default_sessions_dir()
    if args.command == "doctor":
        sessions = [session_summary(path) for path in session_files(sessions_dir)]
        sessions.sort(key=lambda item: (item["timestamp"], item["mtime"]), reverse=True)
        roots = [item for item in sessions if item["thread_source"] != "subagent"]
        data = {
            "version": _version(),
            "python": sys.version.split()[0],
            "sessions_dir": str(Path(sessions_dir).expanduser()),
            "sessions_dir_exists": Path(sessions_dir).expanduser().is_dir(),
            "session_count": len(sessions),
            "newest_root": roots[0] if roots else None,
            "newest_session": sessions[0] if sessions else None,
            "auth_required": False,
        }
        _print_result(data, args.json)
        return

    if args.command == "list":
        data = list_sessions(sessions_dir, limit=args.limit, cwd=args.cwd, thread_source=args.thread_source)
        _print_result(data, args.json)
        return

    local_candidate = Path(args.session).expanduser()
    remote = None if local_candidate.is_file() else parse_remote_reference(args.session)

    if args.command == "tree":
        data = (
            build_remote_tree(remote, args.sessions_dir)
            if remote
            else build_tree(args.session, sessions_dir)
        )
        _print_result(data if args.format == "json" else _tree_text(data), args.json or args.format == "json")
        return

    if args.command == "breakdown":
        data = _breakdown(args.session, sessions_dir, args)
        _print_result(data, args.json)
        return

    source_dir = args.sessions_dir if remote else sessions_dir
    with open_session_source(args.session, source_dir) as source:
        path = source.path
        if args.command == "raw":
            data = _read_raw(path, args.line)
            _print_result(_redact(data) if args.redact else data, args.json)
        elif args.command == "render":
            output = _resolve_output(args.output, path)
            data = _render(path, output, args)
            if source.remote:
                data["source"] = source.remote.display
            _print_result(data, args.json)
        elif args.command == "browser":
            meta = read_session_meta(path)
            session_id = str(meta.get("id") or meta.get("session_id") or path.stem)
            if args.output:
                output = _resolve_output(args.output, path)
            else:
                directory = Path(tempfile.gettempdir()) / "codex-transcript"
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                directory.chmod(0o700)
                name = f"{source.remote.host}-{session_id}" if source.remote else session_id
                output = _resolve_output(directory / f"{_safe_filename(name)}.html", path)
            data = _render(path, output, args)
            if source.remote:
                data["source"] = source.remote.display
            opened = _open_browser(output.as_uri())
            if not opened:
                raise RuntimeError(f"default browser did not accept {output}")
            data["opened"] = True
            _print_result(data, args.json)
        elif args.command in {"export", "query"}:
            output = None if args.output == "-" else _resolve_output(args.output, path)
            count = _emit_events(_selected_events(path, args), args.format, output)
            if output is not None:
                result = {"path": str(output), "events": count}
                if source.remote:
                    result["source"] = source.remote.display
                _print_result(result, args.json)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        run(args)
    except (FileNotFoundError, ValueError, RuntimeError, OSError) as error:
        if args.json:
            print(json.dumps({"ok": False, "error": {"type": type(error).__name__, "message": str(error)}}))
        else:
            print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
