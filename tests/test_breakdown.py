from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from codex_transcript_viewer.breakdown import _command_invocations, _decode_command, build_breakdown
from codex_transcript_viewer import cli
from codex_transcript_viewer.discovery import resolve_session


ROOT_ID = "019f6000-0000-7000-8000-000000000001"
CHILD_ID = "019f6000-1000-7000-8000-000000000002"
ROOT_TURN = "019f6000-2000-7000-8000-000000000003"
CHILD_TURN = "019f6000-3000-7000-8000-000000000004"


def record(timestamp: str, outer_type: str, payload: dict) -> dict:
    return {"timestamp": timestamp, "type": outer_type, "payload": payload}


def write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")


def meta(session_id: str, *, parent: str = "") -> dict:
    source: dict | str = "cli"
    if parent:
        source = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "agent_path": "/root/child"}}}
    return {
        "id": session_id,
        "session_id": session_id,
        "timestamp": "2026-07-11T12:00:00Z",
        "thread_source": "subagent" if parent else "user",
        "source": source,
        "base_instructions": {"text": "x" * 200},
    }


class BreakdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_path = self.root / "2026" / "07" / "11" / "root.jsonl"
        self.child_path = self.root / "2026" / "07" / "11" / "child.jsonl"

        write(self.root_path, [
            record("2026-07-11T12:00:00Z", "session_meta", meta(ROOT_ID)),
            record("2026-07-11T12:00:01Z", "event_msg", {"type": "task_started", "turn_id": ROOT_TURN}),
            record("2026-07-11T12:00:02Z", "response_item", {"type": "function_call", "id": "fc-1", "call_id": "wait", "name": "wait_agent", "arguments": '{"timeout_ms":30000}', "internal_chat_message_metadata_passthrough": {"turn_id": ROOT_TURN}}),
            record("2026-07-11T12:00:04.250Z", "response_item", {"type": "function_call_output", "id": "fo-1", "call_id": "wait", "output": "Wall time 2.1 seconds", "internal_chat_message_metadata_passthrough": {"turn_id": ROOT_TURN}}),
        ])
        write(self.child_path, [
            record("2026-07-11T11:59:00Z", "session_meta", meta(CHILD_ID, parent=ROOT_ID)),
            record("2026-07-11T11:59:01Z", "event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 99, "total_tokens": 99}}}),
            record("2026-07-11T12:00:05Z", "event_msg", {"type": "task_started", "turn_id": CHILD_TURN, "started_at": 1783771205}),
            record("2026-07-11T12:00:06Z", "response_item", {"type": "custom_tool_call", "id": "ctc", "call_id": "exec", "name": "exec", "input": 'await tools.shell_command({command:"Get-Content C:\\\\repo\\\\file.txt; ' + ('x' * 160) + '"});', "internal_chat_message_metadata_passthrough": {"turn_id": CHILD_TURN}}),
            record("2026-07-11T12:00:07Z", "response_item", {"type": "custom_tool_call_output", "id": "ctco", "call_id": "exec", "output": [{"type": "input_text", "text": "ёж"}], "internal_chat_message_metadata_passthrough": {"turn_id": CHILD_TURN}}),
            record("2026-07-11T12:00:08Z", "event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "total_tokens": 100}, "last_token_usage": {"input_tokens": 100}}, "rate_limits": {"limit_id": "codex"}}),
            record("2026-07-11T12:00:09Z", "event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "total_tokens": 100}, "last_token_usage": {"input_tokens": 0}}, "rate_limits": {"limit_id": "codex"}}),
            record("2026-07-11T12:00:10Z", "event_msg", {"type": "turn_aborted", "turn_id": CHILD_TURN, "duration_ms": 5000, "reason": "interrupted"}),
        ])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_breakdown_preserves_raw_events_tokens_and_tool_links_without_metrics(self) -> None:
        data = build_breakdown(ROOT_ID, self.root)
        self.assertEqual(data["root_session_id"], ROOT_ID)
        self.assertEqual([session["session_id"] for session in data["sessions"]], [ROOT_ID, CHILD_ID])
        child = data["sessions"][1]
        self.assertEqual(len(child["events"]), 8)
        self.assertEqual(child["events"][0]["record_origin"], "native")
        inherited = child["events"][1]
        self.assertEqual(inherited["record_origin"], "inherited")
        tokens = [event for event in child["events"] if event["payload_type"] == "token_count"]
        self.assertEqual(len(tokens), 3)
        self.assertEqual(tokens[0]["record_origin"], "inherited")
        self.assertEqual(tokens[2]["details"]["is_duplicate_snapshot"], True)
        self.assertEqual(tokens[2]["details"]["duplicate_of_event_id"], tokens[1]["event_id"])
        self.assertNotIn("metrics", child)
        self.assertNotIn("turns", child)
        self.assertNotIn("tree_metrics", data)

        call = next(event for event in child["events"] if event["kind"] == "tool_call")
        output = next(event for event in child["events"] if event["kind"] == "tool_output")
        self.assertEqual(call["duration"]["observed_ms"], 1000)
        self.assertEqual(output["details"]["output_size"]["text_chars"], 2)
        nested = call["details"]["nested_calls"][0]
        self.assertEqual(nested["tool"], "shell_command")
        self.assertEqual(nested["command_name"], "Get-Content")
        self.assertEqual(len(nested["command_preview"]), 128)
        self.assertEqual(nested["command_sha256"], hashlib.sha256(("Get-Content C:\\repo\\file.txt; " + "x" * 160).encode()).hexdigest())
        self.assertEqual(child["meta"]["base_instructions"]["text"]["chars"], 200)

    def test_tool_pair_duration_and_unique_basename_resolution(self) -> None:
        data = build_breakdown("root.jsonl", self.root)
        call = next(event for event in data["sessions"][0]["events"] if event["kind"] == "tool_call")
        self.assertEqual(call["duration"]["observed_ms"], 2250)
        self.assertEqual(call["duration"]["reported_ms"], 2100)
        self.assertEqual(call["duration"]["source"], "call_to_output_timestamp")
        self.assertEqual(resolve_session("root.jsonl", self.root), self.root_path.resolve())

    def test_decode_command_accepts_unquoted_javascript_object_keys(self) -> None:
        self.assertEqual(_decode_command('{command:"Get-Content C:\\\\repo\\\\file.txt"}'), r"Get-Content C:\repo\file.txt")
        self.assertEqual(_decode_command("{command:'Get-Content C:\\\\repo\\\\file.txt'}"), r"Get-Content C:\repo\file.txt")

    def test_decode_command_preserves_template_literal_interpolation(self) -> None:
        command = _decode_command(r"{command:`wam_mplan find --request ${req} --scope ${scope}`}")
        self.assertEqual(command, "wam_mplan find --request ${req} --scope ${scope}")
        self.assertEqual(command.split()[0], "wam_mplan")

    def test_nested_command_projection_handles_python_git_and_wam_mplan(self) -> None:
        python = _command_invocations("@'\nfrom pathlib import Path\n'@ | & 'C:\\repo\\venv\\Scripts\\python.exe' -")
        self.assertEqual(python[0]["command_name"], "python")
        self.assertEqual(python[0]["command_kind"], "python_stdin_script")

        git = _command_invocations("git diff --numstat -- file.txt; git status --short")
        self.assertEqual([item["command_operation"] for item in git], ["diff", "status"])
        self.assertEqual(git[0]["command_label"], "git diff --numstat -- file.txt")

        wam = _command_invocations("wam_mplan find master-plan/flow.yaml --where state=ready --limit 10 --collection records --format json")[0]
        self.assertEqual(wam["command_operation"], "find")
        self.assertEqual(wam["store_path"], "master-plan/flow.yaml")
        self.assertEqual(wam["where"], ["state=ready"])
        self.assertEqual(wam["limit"], "10")

    def test_command_projection_handles_windows_paths_and_wam_options(self) -> None:
        python = _command_invocations("@'\nfrom pathlib import Path\n'@ | & 'C:\\Program Files\\Python\\python.exe' -")
        self.assertEqual(python[0]["command_name"], "python")
        self.assertEqual(python[0]["command_path"], r"C:\Program Files\Python\python.exe")

        git = _command_invocations("git -C repo --no-pager status --short")[0]
        self.assertEqual(git["command_operation"], "status")
        self.assertEqual(git["git_global_options"], ["-C", "repo", "--no-pager"])

        wam = _command_invocations("wam_mplan.exe get master-plan/flow.yaml EXE-1 --format json")[0]
        self.assertEqual(wam["identities"], ["EXE-1"])
        self.assertEqual(wam["format"], "json")

    def test_cli_emits_dataset_for_unique_basename(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            cli.main(["--sessions-dir", str(self.root), "breakdown", "root.jsonl", "--output", "-"])
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["root_session_id"], ROOT_ID)
        self.assertEqual(len(data["sessions"]), 2)

    def test_cli_default_output_writes_to_cwd_and_reports_path(self) -> None:
        output = self.root / f"{ROOT_ID}-breakdown.json"
        stdout = io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with redirect_stdout(stdout):
                cli.main(["--sessions-dir", str(self.root), "breakdown", "root.jsonl"])
        finally:
            os.chdir(previous)
        self.assertTrue(output.is_file())
        self.assertIn(str(output), stdout.getvalue())
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["root_session_id"], ROOT_ID)


if __name__ == "__main__":
    unittest.main()
