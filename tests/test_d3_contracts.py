from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class D3ContractTests(unittest.TestCase):
    def test_bundle_version_is_assigned_without_marking_stable(self):
        release = json.loads(
            (ROOT / "release" / "bundle-release.json").read_text(encoding="utf-8")
        )
        self.assertEqual("2.1.0", release["bundle_version"])
        self.assertEqual("unreleased_candidate", release["release_state"])

    def test_d3_schemas_close_receipt_manifest_and_signature_contracts(self):
        receipt = json.loads(
            (ROOT / "contracts" / "d3-candidate-receipt.schema.json").read_text(
                encoding="utf-8"
            )
        )
        manifest = json.loads(
            (ROOT / "contracts" / "d3-release-manifest.schema.json").read_text(
                encoding="utf-8"
            )
        )
        signing_policy = json.loads(
            (ROOT / "contracts" / "release-signing-policy.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("receipt_sha256", receipt["required"])
        self.assertEqual(
            "d3-candidate-acceptance",
            receipt["properties"]["receipt_type"]["const"],
        )
        self.assertIn("required_signature", manifest["required"])
        self.assertEqual(
            "RSA-SHA256-PKCS1-v1_5",
            manifest["properties"]["required_signature"]["properties"]["algorithm"]["const"],
        )
        self.assertIn(
            "key_id",
            manifest["properties"]["required_signature"]["required"],
        )
        self.assertEqual(
            "encrypted-pkcs8-dpapi-current-user",
            signing_policy["properties"]["private_key_storage"]["const"],
        )

    def test_d3_documentation_keeps_external_execution_and_publish_boundaries_explicit(self):
        contract = (ROOT / "contracts" / "d3-release-candidate-v1.md").read_text(
            encoding="utf-8"
        )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for text in (
            "macos-15-intel",
            "macos-15",
            "GitHub artifact attestation",
            "release/d3_candidate.py",
            "release/d3_release.py",
            "不 tag、push、创建 Release 或切换 stable",
            "生产签名私钥和 DPAPI 密文不得进入仓库",
            "生产公钥指纹",
            "DPAPI",
        ):
            self.assertIn(text, contract)
        release = json.loads(
            (ROOT / "release" / "bundle-release.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            "montewaltrip188-hash/obsidian-wiki-setup",
            release["repositories"]["installer"],
        )
        self.assertIn("bundle `2.1.0`", readme)
        self.assertIn("run_approval_required", readme)
        self.assertIn("D3", readme)


if __name__ == "__main__":
    unittest.main()
