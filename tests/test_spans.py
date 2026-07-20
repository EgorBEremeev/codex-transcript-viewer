from __future__ import annotations

import unittest

from codex_transcript_viewer.spans import SPAN_ANALYSIS_VERSION, breakdown_sha256, build_spans


ROOT = "root-session"
CHILD = "child-session"
TURN = "turn-1"


def event(event_id: str, at: int, kind: str, *, turn_id: str = "", details: dict | None = None, duration: dict | None = None) -> dict:
    return {
        "event_id": event_id,
        "line": int(event_id.rsplit(":", 1)[-1]),
        "seq": int(event_id.rsplit(":", 1)[-1]),
        "timestamp": f"2026-07-20T00:00:{at:02d}.000Z",
        "timestamp_ms": at * 1000,
        "outer_type": "response_item",
        "payload_type": kind,
        "kind": kind,
        "turn_id": turn_id,
        "record_origin": "native",
        "duration": duration or {"observed_ms": None, "reported_ms": None, "source": None},
        "details": {"payload_size": {"serialized_json_utf8_bytes": 10}, **(details or {})},
    }


def fixture() -> dict:
    call = event(
        f"{ROOT}:2", 2, "tool_call", turn_id=TURN,
        duration={"observed_ms": 1000, "reported_ms": None, "source": "call_to_output_timestamp"},
        details={
            "name": "exec", "call_id": "call-1", "output_event_ids": [f"{ROOT}:3"],
            "input_size": {"serialized_json_utf8_bytes": 20},
            "nested_calls": [{"tool": "shell_command", "command_name": "Get-Content", "extraction": {"method": "test"}}],
        },
    )
    output = event(f"{ROOT}:3", 3, "tool_output", turn_id=TURN, details={"call_id": "call-1", "paired_call_event_id": f"{ROOT}:2", "output_size": {"serialized_json_utf8_bytes": 30}})
    root_events = [event(f"{ROOT}:1", 1, "task_started", turn_id=TURN), call, output, event(f"{ROOT}:4", 4, "token_count", turn_id=TURN, details={"info": {"total_token_usage": {"input_tokens": 100}, "last_token_usage": {"input_tokens": 100}, "model_context_window": 258400}}), event(f"{ROOT}:5", 5, "task_complete", turn_id=TURN)]
    child_events = [event(f"{CHILD}:1", 6, "task_started", turn_id="child-turn"), event(f"{CHILD}:2", 7, "turn_aborted", turn_id="child-turn")]
    return {
        "schema_version": 1, "root_session_id": ROOT, "timeline": [], "tree_metrics": {}, "warnings": [],
        "sessions": [
            {"session_id": ROOT, "parent_session_id": "", "agent_path": "", "meta": {"thread_source": "user"}, "record_scope": {}, "events": root_events, "turns": [{"turn_id": TURN, "start_event_id": f"{ROOT}:1", "end_event_id": f"{ROOT}:5", "duration_ms": 4000, "outcome": "completed", "time_to_first_token_ms": 50}]},
            {"session_id": CHILD, "parent_session_id": ROOT, "agent_path": "/root/executor", "meta": {"agent_role": "wam-executor", "thread_source": "subagent"}, "record_scope": {}, "events": child_events, "turns": [{"turn_id": "child-turn", "start_event_id": f"{CHILD}:1", "end_event_id": f"{CHILD}:2", "duration_ms": 1000, "outcome": "aborted", "time_to_first_token_ms": None}]},
        ],
    }


class SpanTests(unittest.TestCase):
    def test_builds_session_turn_and_tool_spans_without_copying_events(self) -> None:
        data = fixture()
        result = build_spans(data)
        spans = {span["span_id"]: span for span in result["spans"]}
        self.assertEqual(result["analysis_version"], SPAN_ANALYSIS_VERSION)
        self.assertEqual(result["source"]["breakdown_sha256"], breakdown_sha256(data))
        self.assertEqual(spans[f"session:{CHILD}"]["parent_span_id"], f"session:{ROOT}")
        self.assertEqual(spans[f"turn:{ROOT}:{TURN}"]["attributes"]["outcome"], "completed")
        tool = spans[f"tool:{ROOT}:2"]
        self.assertEqual(tool["duration_ms"], 1000)
        self.assertEqual(tool["event_ids"], [f"{ROOT}:2", f"{ROOT}:3"])
        self.assertEqual(tool["attributes"]["nested_calls"][0]["command_name"], "Get-Content")
        self.assertEqual(tool["attributes"]["payload_total_bytes"], 20)
        self.assertIn(tool["span_id"], result["event_to_span"][f"{ROOT}:3"])
        self.assertNotIn("details", tool)

    def test_unpaired_tool_is_preserved_with_warning(self) -> None:
        data = fixture()
        data["sessions"][0]["events"][1]["details"]["output_event_ids"] = []
        result = build_spans(data)
        tool = next(span for span in result["spans"] if span["kind"] == "tool")
        self.assertIsNone(tool["end_event_id"])
        self.assertTrue(result["warnings"])

    def test_spans_always_include_the_complete_breakdown(self) -> None:
        data = fixture()
        result = build_spans(data)
        self.assertEqual(result["excluded_event_count"], 0)
        self.assertIn(f"{ROOT}:2", result["event_to_span"])
        self.assertIn(f"{ROOT}:3", result["event_to_span"])
        root_span = next(span for span in result["spans"] if span["span_id"] == f"session:{ROOT}")
        self.assertEqual(root_span["start_event_id"], f"{ROOT}:1")
        self.assertTrue(any(span["kind"] == "tool" and span["session_id"] == ROOT for span in result["spans"]))


if __name__ == "__main__":
    unittest.main()
