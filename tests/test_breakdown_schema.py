from __future__ import annotations

import json
import unittest
from pathlib import Path


SCHEMA = Path(__file__).parents[1] / "src" / "schemas" / "breakdown.schema.json"


class BreakdownSchemaTests(unittest.TestCase):
    def test_schema_is_valid_json_and_declares_current_version(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["properties"]["schema_version"], {"const": 1})
        self.assertIn("sessions", schema["required"])
        self.assertIn("events", schema["$defs"]["session"]["required"])


if __name__ == "__main__":
    unittest.main()
