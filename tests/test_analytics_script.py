from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


_SCRIPT = Path(__file__).parents[1] / "scripts" / "analytics" / "session_input_tokens_table.py"
_SPEC = importlib.util.spec_from_file_location("session_input_tokens_table", _SCRIPT)
assert _SPEC and _SPEC.loader
table = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(table)


class SessionInputTokensTableTests(unittest.TestCase):
    def test_renders_thread_source_agent_path_and_input_tokens(self) -> None:
        data = {
            "sessions": [
                {
                    "meta": {"thread_source": "user"},
                    "agent_path": "",
                    "metrics": {"reported_token_usage": {"last_cumulative": {"input_tokens": 1200}}},
                },
                {
                    "meta": {"thread_source": "subagent"},
                    "agent_path": "/root/planner",
                    "metrics": {"reported_token_usage": {"input_tokens": {"input_tokens": 34_567}}},
                },
            ]
        }
        self.assertEqual(
            table.markdown_table(data),
            "| Session | Input tokens |\n"
            "| --- | ---: |\n"
            "| user | 1,200 |\n"
            "| subagent [/root/planner] | 34,567 |",
        )


if __name__ == "__main__":
    unittest.main()
