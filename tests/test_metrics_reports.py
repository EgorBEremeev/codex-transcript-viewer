from __future__ import annotations

import json
import csv
import io
import unittest

from codex_transcript_viewer.metrics import build_sessions_metrics
from codex_transcript_viewer.reports import build_session_events_table_csv, build_sessions_table_json
from codex_transcript_viewer.spans import build_spans
from tests.test_spans import ROOT, TURN, event, fixture


class MetricsAndReportsTests(unittest.TestCase):
    def test_metrics_and_csv_reports_are_derived_without_copying_raw_events(self) -> None:
        data = fixture()
        token_one = data["sessions"][0]["events"][3]
        token_one["details"]["rate_limits"] = {"primary": {"used_percent": 10.0}}
        token_two = event(
            f"{ROOT}:6", 6, "token_count", turn_id=TURN,
            details={"info": {"total_token_usage": {"input_tokens": 125}}, "rate_limits": {"primary": {"used_percent": 13.5}}},
        )
        reasoning = event(f"{ROOT}:7", 7, "reasoning", turn_id=TURN)
        data["sessions"][0]["events"].extend([token_two, reasoning])
        spans = build_spans(data)
        metrics = build_sessions_metrics(data, spans)
        root = metrics["sessions"][0]

        self.assertEqual(root["metrics"]["rate_limits"]["primary"]["used_percent"], 3.5)
        self.assertEqual(root["metrics"]["total_tool_time_ms"], 1000)
        self.assertEqual(root["metrics"]["total_reasoning_time_ms"], 1000)
        self.assertNotIn("events", root)
        self.assertEqual(metrics["source"]["root_session_id"], ROOT)

        events_csv = build_session_events_table_csv(data["sessions"][0], spans, until_ms=6500)
        self.assertIn("span_kind", events_csv)
        self.assertIn("tool", events_csv)
        self.assertNotIn("root-session:7", events_csv)
        self.assertNotIn("session_id", events_csv.splitlines()[0])
        self.assertNotIn("agent_path", events_csv.splitlines()[0])
        sessions_report = build_sessions_table_json(metrics)
        self.assertEqual(sessions_report["report_kind"], "sessions_table")
        self.assertEqual(sessions_report["sessions"][0]["events"], root["metrics"]["events"])
        self.assertEqual(sessions_report["tree"]["reported_token_usage"]["sum_session_cumulative"]["input_tokens"], 125)
        self.assertNotIn("snapshot_count", sessions_report["sessions"][0]["reported_token_usage"])
        self.assertNotIn("resets", sessions_report["sessions"][0]["reported_token_usage"])
        self.assertNotIn("native", sessions_report["sessions"][0]["context_material"])
        json.dumps(metrics)
        json.dumps(sessions_report)

    def test_events_csv_clips_tool_crossing_until_boundary(self) -> None:
        data = fixture()
        spans = build_spans(data)
        rows = list(csv.DictReader(io.StringIO(build_session_events_table_csv(data["sessions"][0], spans, until_ms=2500))))
        tool = next(row for row in rows if row["span_kind"] == "tool")
        self.assertEqual(tool["duration_ms"], "500")
        self.assertEqual(tool["payload_output_bytes"], "")
        self.assertEqual(tool["payload_cumulative_bytes"], "20")

    def test_session_events_csv_keeps_a_header_after_full_cutoff(self) -> None:
        data = fixture()
        spans = build_spans(data)
        content = build_session_events_table_csv(data["sessions"][1], spans, until_ms=0)
        self.assertEqual(len(content.splitlines()), 1)
        self.assertNotIn("session_id", content)
        self.assertNotIn("agent_path", content)


if __name__ == "__main__":
    unittest.main()
