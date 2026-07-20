from __future__ import annotations

import io
import json
import tempfile
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
            self.assertTrue(spans.is_file())
            viewer = root / "trace.html"
            with redirect_stdout(stdout):
                cli.main(["visualize", str(breakdown), "--spans", str(spans), "--output", str(viewer)])
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

    def test_cli_analyze_rejects_invalid_local_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            breakdown = Path(temp) / "breakdown.json"
            breakdown.write_text(json.dumps(fixture()), encoding="utf-8")
            with self.assertRaises(SystemExit):
                cli.main(["analyze", str(breakdown), "--since", "2026-07-20T12:00:00"])


if __name__ == "__main__":
    unittest.main()
