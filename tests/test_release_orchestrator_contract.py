from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseOrchestratorContractTests(unittest.TestCase):
    def test_plan_schema_and_contract_freeze_read_only_public_seams(self):
        schema = json.loads(
            (ROOT / "contracts" / "release-orchestrator-plan.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            ["planned"], schema["properties"]["status"]["enum"]
        )
        contract = (ROOT / "contracts" / "release-orchestrator-v1.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("orchestrator.py plan", contract)
        self.assertIn("orchestrator.py status", contract)
        self.assertIn("不 commit、tag、push", contract)
        self.assertIn("version_approval_required", contract)


if __name__ == "__main__":
    unittest.main()
