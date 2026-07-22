from __future__ import annotations

import json
import csv
import io
import unittest

from codex_transcript_viewer.metrics import build_sessions_metrics
from codex_transcript_viewer.reports import build_events_table_csv, build_sessions_table_csv
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

        sessions_csv = build_sessions_table_csv(metrics)
        events_csv = build_events_table_csv(data, spans, until_ms=6500)
        self.assertIn("rate_limit_primary_used_percent", sessions_csv)
        self.assertIn("tree", sessions_csv)
        self.assertIn("span_kind", events_csv)
        self.assertIn("tool", events_csv)
        self.assertNotIn("root-session:7", events_csv)
        session_rows = list(csv.DictReader(io.StringIO(sessions_csv)))
        self.assertEqual(session_rows[-1]["scope"], "tree")
        self.assertEqual(session_rows[-1]["token_input_tokens"], "125")
        json.dumps(metrics)

    def test_events_csv_clips_tool_crossing_until_boundary(self) -> None:
        data = fixture()
        spans = build_spans(data)
        rows = list(csv.DictReader(io.StringIO(build_events_table_csv(data, spans, until_ms=2500))))
        tool = next(row for row in rows if row["span_kind"] == "tool")
        self.assertEqual(tool["duration_ms"], "500")
        self.assertEqual(tool["payload_output_bytes"], "")
        self.assertEqual(tool["payload_cumulative_bytes"], "20")


if __name__ == "__main__":
    unittest.main()
