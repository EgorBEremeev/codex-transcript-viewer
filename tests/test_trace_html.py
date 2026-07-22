from __future__ import annotations

import io
import json
import tempfile
import shutil
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from codex_transcript_viewer import cli
from codex_transcript_viewer.spans import build_spans
from codex_transcript_viewer.trace_html_builder import build_trace_html
from tests.test_spans import fixture


class TraceHtmlTests(unittest.TestCase):
    def test_html_embeds_safe_data_and_inline_assets(self) -> None:
        data = fixture()
        data["sessions"][0]["events"][0]["details"]["note"] = "</script><script>alert(1)</script>"
        html = build_trace_html(data, build_spans(data))
        self.assertIn('id="breakdown-data"', html)
        self.assertIn('id="spans-data"', html)
        self.assertIn("trace-canvas", html)
        self.assertIn("isClipped", html)
        self.assertIn("tool&&!clipped", html)
        self.assertIn('call && (call.command_label || call.command_name)', html)
        self.assertIn('return calls.length ? [name, ...calls].join(" → ") : name;', html)
        self.assertIn("Get-Content", html)
        self.assertIn("Select-String", html)
        self.assertIn('id="download-events-csv"', html)
        self.assertIn("downloadVisibleEventsCsv", html)
        self.assertIn('event.kind === "sub_agent_activity"', html)
        self.assertIn('points:payloadPoints,alwaysVisible:true', html)
        self.assertIn('(!item.alwaysVisible && !state.enabled.has(item.key))', html)
        self.assertIn("\\u003c/script", html)
        self.assertEqual(html.count("</script>"), 3)
        embedded = html.split('id="breakdown-data" type="application/json">', 1)[1].split("</script>", 1)[0]
        self.assertEqual(json.loads(embedded)["root_session_id"], "root-session")

    def test_cli_analyze_and_visualize(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            breakdown = root / "breakdown.json"
            breakdown.write_text(json.dumps(fixture()), encoding="utf-8")
            analysis_dir = root / "analysis"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                cli.main(["analyze", str(breakdown), "--output", str(analysis_dir)])
            spans = analysis_dir / "spans.json"
            trace = analysis_dir / "trace.html"
            self.assertTrue(spans.is_file())
            self.assertTrue(trace.is_file())
            self.assertTrue((analysis_dir / "root-session-breakdown.json").is_file())
            self.assertTrue((analysis_dir / "root-session-sessions-metrics.json").is_file())
            self.assertTrue((analysis_dir / "sessions_table.csv").is_file())
            self.assertTrue((analysis_dir / "events_table.csv").is_file())
            viewer = root / "trace.html"
            with redirect_stdout(stdout):
                cli.main(["visualize", "--spans", str(analysis_dir), "--output", str(viewer)])
            self.assertTrue(viewer.is_file())
            self.assertIn("Trace", viewer.read_text(encoding="utf-8"))

    def test_cli_rejects_spans_from_other_breakdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = fixture(); second = fixture(); second["root_session_id"] = "other-root"
            first_path, second_path, spans_path = root / "first.json", root / "second.json", root / "spans.json"
            first_path.write_text(json.dumps(first), encoding="utf-8")
            second_path.write_text(json.dumps(second), encoding="utf-8")
            spans_path.write_text(json.dumps(build_spans(first)), encoding="utf-8")
            with self.assertRaises(SystemExit):
                cli.main(["visualize", str(second_path), "--spans", str(spans_path), "--output", str(root / "out.html")])

    def test_cli_analyze_stores_until_without_filtering_spans(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            breakdown = Path(temp) / "breakdown.json"
            breakdown.write_text(json.dumps(fixture()), encoding="utf-8")
            output = Path(temp) / "analysis"
            with redirect_stdout(io.StringIO()):
                cli.main(["analyze", str(breakdown), "--until", "2026-07-20:00:00:03", "--output", str(output)])
            analysis = json.loads((output / "spans.json").read_text(encoding="utf-8"))
            self.assertEqual(analysis["included_event_count"], 7)
            self.assertIn("until_ms", analysis["viewer_defaults"])

    def test_visualize_uses_sibling_breakdown_after_analysis_directory_moves(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            breakdown = root / "breakdown.json"
            breakdown.write_text(json.dumps(fixture()), encoding="utf-8")
            first = root / "first"
            moved = root / "moved"
            with redirect_stdout(io.StringIO()):
                cli.main(["analyze", str(breakdown), "--output", str(first)])
            shutil.move(str(first), moved)
            output = root / "trace.html"
            with redirect_stdout(io.StringIO()):
                cli.main(["visualize", "--spans", str(moved), "--output", str(output)])
            self.assertTrue(output.is_file())

    def test_cli_analyze_rejects_invalid_until(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            breakdown = Path(temp) / "breakdown.json"
            breakdown.write_text(json.dumps(fixture()), encoding="utf-8")
            with self.assertRaises(SystemExit):
                cli.main(["analyze", str(breakdown), "--until", "2026-07-20T12:00:00"])


if __name__ == "__main__":
    unittest.main()
