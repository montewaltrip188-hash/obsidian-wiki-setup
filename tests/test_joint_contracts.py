from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"


class JointContractTests(unittest.TestCase):
    def load(self, name: str) -> dict:
        return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))

    def test_public_schemas_are_strict_and_match_the_two_index_actions(self):
        plan = self.load("joint-update-plan.schema.json")
        approval = self.load("joint-update-approval.schema.json")
        receipt = self.load("joint-update-receipt.schema.json")

        self.assertFalse(plan["additionalProperties"])
        self.assertFalse(approval["additionalProperties"])
        self.assertFalse(receipt["additionalProperties"])
        self.assertEqual(
            ["none", "full_rebuild"],
            plan["properties"]["index_plan"]["properties"]["index_action"]["enum"],
        )
        self.assertEqual(
            ["apply", "apply_failure", "rollback"],
            receipt["properties"]["operation"]["enum"],
        )

    def test_candidate_contract_lists_every_joint_public_entry(self):
        text = (CONTRACTS / "install-candidate-v1.md").read_text(encoding="utf-8")
        for relative in (
            "tools/joint_update.py",
            "scripts/joint-update.ps1",
            "scripts/joint-update.sh",
            "joint-update-plan.schema.json",
            "joint-update-approval.schema.json",
            "joint-update-receipt.schema.json",
        ):
            self.assertIn(relative, text)


if __name__ == "__main__":
    unittest.main()
