"""Print a Markdown table of reported input tokens by breakdown session."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _input_tokens(session: dict[str, Any]) -> int | float | None:
    reported = session.get("metrics", {}).get("reported_token_usage", {})
    usage = reported.get("last_cumulative", {}) if isinstance(reported, dict) else {}
    value = usage.get("input_tokens") if isinstance(usage, dict) else None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        nested = value.get("input_tokens")
        return nested if isinstance(nested, (int, float)) else None
    legacy = reported.get("input_tokens") if isinstance(reported, dict) else None
    if isinstance(legacy, dict):
        nested = legacy.get("input_tokens")
        return nested if isinstance(nested, (int, float)) else None
    return None


def _session_label(session: dict[str, Any]) -> str:
    thread_source = str(session.get("meta", {}).get("thread_source") or "unknown")
    agent_path = str(session.get("agent_path") or "")
    return f"{thread_source} [{agent_path}]" if agent_path else thread_source


def markdown_table(data: dict[str, Any]) -> str:
    rows = ["| Session | Input tokens |", "| --- | ---: |"]
    for session in data.get("sessions", []):
        value = _input_tokens(session)
        display = f"{value:,}" if isinstance(value, (int, float)) else "—"
        rows.append(f"| {_session_label(session)} | {display} |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Print reported input-token usage by session as a Markdown table."
    )
    parser.add_argument("breakdown", type=Path, help="path to a breakdown JSON file")
    args = parser.parse_args(argv)
    with args.breakdown.open(encoding="utf-8") as source:
        data = json.load(source)
    print(markdown_table(data))


if __name__ == "__main__":
    main()
