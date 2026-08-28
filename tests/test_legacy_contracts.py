from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools.install_candidate import INSTALLER_COMMON_REQUIRED


ROOT = Path(__file__).resolve().parents[1]


class LegacyContractTests(unittest.TestCase):
    def test_candidate_requires_all_legacy_adoption_schemas(self):
        required = {
            "contracts/legacy-catalog.schema.json",
            "contracts/legacy-adoption-plan.schema.json",
            "contracts/legacy-adoption-approval.schema.json",
            "contracts/legacy-adoption-receipt.schema.json",
        }
        self.assertTrue(required <= INSTALLER_COMMON_REQUIRED)
        for relative in required:
            payload = json.loads((ROOT / relative).read_text(encoding="utf-8"))
            self.assertFalse(payload["additionalProperties"])

    def test_public_docs_freeze_manual_adoption_gate_and_customer_boundary(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        contract = (ROOT / "contracts" / "legacy-adoption-v1.md").read_text(encoding="utf-8")
        for text in (readme, contract):
            self.assertIn("legacy-plan", text)
            self.assertIn("legacy-adopt", text)
            self.assertIn("人工", text)
        self.assertIn("不扫描客户内容目录", contract)


if __name__ == "__main__":
    unittest.main()
