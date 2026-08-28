from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOM = ROOT / "contracts" / "offline-keyword-runtime-bom.json"
SCHEMA = ROOT / "contracts" / "offline-keyword-runtime-bom.schema.json"


class OfflineKeywordRuntimeContractTests(unittest.TestCase):
    def test_bom_freezes_approved_runtime_platforms_dependencies_and_policy(self):
        bom = json.loads(BOM.read_text(encoding="utf-8"))

        self.assertEqual(1, bom["schema_version"])
        self.assertEqual("cpython-3.12.14+20260825", bom["runtime_id"])
        self.assertEqual("astral-sh/python-build-standalone", bom["provider"])
        self.assertEqual("20260825", bom["provider_release"])
        self.assertEqual("3.12.14", bom["python_version"])
        self.assertEqual("install_only", bom["archive_flavor"])
        self.assertFalse(bom["policy"]["client_network_install"])
        self.assertFalse(bom["policy"]["modify_system_python"])
        self.assertFalse(bom["policy"]["modify_customer_content"])
        self.assertTrue(bom["policy"]["require_asset_sha256"])
        self.assertTrue(bom["policy"]["require_licenses"])
        self.assertTrue(bom["policy"]["require_sbom"])
        self.assertEqual(
            {"windows-x64", "macos-x64", "macos-arm64"},
            set(bom["targets"]),
        )
        self.assertEqual(
            {
                "certifi": "2026.7.22",
                "charset-normalizer": "3.5.1",
                "idna": "3.19",
                "jieba": "0.42.1",
                "numpy": "2.5.2",
                "requests": "2.34.2",
                "urllib3": "2.7.0",
            },
            bom["locked_versions"],
        )
        self.assertEqual("sdist_vendored", bom["packages"]["jieba"]["install_mode"])
        self.assertEqual(
            "055ca12f62674fafed09427f176506079bc135638a14e23e25be909131928db2",
            bom["packages"]["jieba"]["asset"]["sha256"],
        )
        self.assertTrue(SCHEMA.is_file())


if __name__ == "__main__":
    unittest.main()
