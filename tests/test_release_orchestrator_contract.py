from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseOrchestratorContractTests(unittest.TestCase):
    def test_offline_keyword_runtime_gate_is_ready_and_exactly_bound(self):
        lifecycle = json.loads(
            (ROOT / "contracts" / "wiki-skill-lifecycle.json").read_text(
                encoding="utf-8"
            )
        )
        defaults = lifecycle["defaults"]
        policy = lifecycle["dependency_policy"]
        self.assertTrue(defaults["keyword_runtime_ready"])
        self.assertEqual("ready", defaults["keyword_runtime_status"])
        self.assertIsNone(defaults["keyword_runtime_error"])
        self.assertEqual("cpython-3.12.14+20260825", defaults["keyword_runtime_id"])
        self.assertEqual(
            ["windows-x64", "macos-x64", "macos-arm64"],
            defaults["keyword_runtime_targets"],
        )
        self.assertFalse(policy["automatic_network_install"])
        self.assertEqual("forbidden", policy["client_package_install"])
        self.assertEqual("forbidden", policy["system_python_modification"])

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
        self.assertIn("runtime_provisioning_required", contract)


if __name__ == "__main__":
    unittest.main()
